import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from job_search import collect_jobs, fit_work, refresh_queue, refresh_worker
from tests.test_collect_jobs import posting
from tests.test_fit_work import response
from tests.test_job_fit import assessment, criterion


class ReviewAnswerTests(unittest.TestCase):
    question = "운영 장애에 직접 대응한 경험이 있나요?"
    answer = "합성 테스트 전용 답변 내용"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.jobs = Path(self.directory.name) / "jobs.db"
        self.state = self.jobs.with_name("state.db")
        self.profile = {"P1": "합성 경력"}
        body = ('<p>운영 장애 대응 경험이 필수입니다.</p><p>지원 마감 2099-01-01</p>'
                '<a href="https://example.org/apply">지원하러 가기</a>')
        collect_jobs.collect(self.jobs, lambda _: ([posting(1, body), posting(2, body)], 1))
        self.doc = collect_jobs.get_posts(self.jobs, [1])[0]
        fit = assessment(criterion("운영 장애 대응", "required", "unknown"), question=self.question)
        entry = dict(id=1, fingerprint=self.doc["fingerprint"], decision="needs_review",
                     reason="필수 경험 확인", evidence_lines=[1], assessment=fit, availability="open",
                     deadline="2099-01-01", apply_url=self.doc["apply_url"], required_summary="운영 장애 대응")
        collect_jobs.record_reviews(self.jobs, [entry], execution_id="legacy")
        refresh_worker.migrate(self.jobs, self.state, schema_only=True)

    def submit(self, **fields):
        from job_search import review_answers
        params = dict(fingerprint=self.doc["fingerprint"], question=self.question, answer=self.answer)
        return review_answers.submit(self.jobs, self.state, 1, **{**params, **fields})

    def test_private_schema_survives_redeploy_and_is_absent_from_collector(self):
        with closing(sqlite3.connect(self.state)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        self.assertIn("review_answers", tables)
        run = self.submit()
        refresh_worker.migrate(self.jobs, self.state, schema_only=True)
        self.assertEqual(self.submit(), run)
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='review_answers'").fetchone())

    def test_duplicate_concurrent_requests_enqueue_only_one_fit_and_validate_current_question(self):
        from job_search import review_answers
        with self.assertRaises(review_answers.AnswerChanged):
            self.submit(question="다른 질문인가요?")
        with self.assertRaises(review_answers.AnswerChanged):
            self.submit(fingerprint="obsolete")
        for answer in (" ", "x" * 2001):
            with self.assertRaises(ValueError):
                self.submit(answer=answer)
        with ThreadPoolExecutor(2) as pool:
            runs = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(runs[0], runs[1])
        with closing(sqlite3.connect(self.state)) as db:
            # Rollback workers join a deadline task before claiming fit work. The answer waits for upgrade.
            legacy = db.execute("""SELECT w.id FROM refresh_work w
                JOIN refresh_work d ON d.group_id=w.group_id AND d.kind='deadline'
                WHERE w.kind='fit' AND w.status='queued'""").fetchall()
        self.assertEqual(legacy, [])
        self.assertFalse(refresh_queue.claim(self.state, "deadline", 1))
        self.assertFalse(refresh_queue.claim(self.state, "logo", 1))
        self.assertEqual([t["post_id"] for t in refresh_queue.claim(self.state, "fit", 5)], [1])
        self.assertEqual(self.submit(), runs[0])

    def test_single_job_evidence_changes_score_and_is_absent_from_shared_outputs(self):
        run = self.submit()
        tasks = refresh_queue.claim(self.state, "fit", 5)
        tasks += [dict(id="cron:2", post_id=2)]
        batches = []

        def evaluate(attempt, profile, docs):
            batches.append([d["id"] for d in docs])
            result = response(attempt, profile, docs)
            for doc, entry in zip(docs, result["entries"]):
                if doc["id"] == 1:
                    facts = doc["answer_facts"]
                    self.assertIn(self.answer, facts["A0001"])
                    entry["assessment"]["criteria"][0].update(
                        profile_ids=["A0001"], evidence=self.answer, label=self.answer, key=self.answer
                    )
                    entry["reason"] = entry["required_summary"] = entry["sensitive_extra"] = self.answer
                else:
                    self.assertNotIn(self.answer, json.dumps([docs, profile], ensure_ascii=False))
            return result

        def finish(task, status, message):
            self.assertEqual(status, "done", message)
            if task["post_id"] == 1:
                refresh_queue.finish(self.state, task, status, message)

        fit_work.refresh_fit_tasks(self.jobs, self.state, tasks, finish, profile=self.profile, runner=evaluate)
        self.assertEqual(batches, [[1], [2]])
        self.assertEqual(self.submit(), run)
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["fit_review"]["score"], 100)
        with closing(sqlite3.connect(self.jobs)) as db:
            for line in db.iterdump():
                self.assertNotIn(self.answer, line)
                self.assertNotIn(json.dumps(self.answer)[1:-1], line)
        for path in Path(str(self.jobs) + ".fit-runs").glob("*"):
            self.assertNotIn(self.answer.encode(), path.read_bytes())

    def test_durable_answer_does_not_redeliver_an_unresolved_question(self):
        self.submit()
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE fit_delivery SET execution_id=NULL WHERE post_id=1")

        def unresolved(attempt, profile, docs):
            result = response(attempt, profile, docs)
            entry = result["entries"][0]
            entry["decision"] = "needs_review"
            entry["assessment"]["question"] = self.question
            entry["assessment"]["criteria"][0].update(status="unknown", profile_ids=[])
            return result

        digest = fit_work.scheduled_review(self.jobs, self.state, "unresolved", ids=[1],
                                           profile=self.profile, runner=unresolved)
        self.assertEqual(digest, "[SILENT]")

    def test_redeploy_recovers_scored_answer_overwritten_by_an_older_worker(self):
        with closing(sqlite3.connect(self.jobs)) as db:
            old = db.execute("SELECT decision,fit_review FROM posts WHERE id=1").fetchone()
        run = self.submit()
        task, = refresh_queue.claim(self.state, "fit", 1)
        fit_work.refresh_fit_tasks(self.jobs, self.state, [task],
                                   lambda *args: refresh_queue.finish(self.state, *args),
                                   profile=self.profile, runner=response)
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision=?,fit_review=? WHERE id=1", old)
        refresh_worker.migrate(self.jobs, self.state, schema_only=True)
        task, = refresh_queue.claim(self.state, "fit", 1)
        self.assertEqual(task["run_id"], run)
        fit_work.refresh_fit_tasks(self.jobs, self.state, [task],
                                   lambda *args: refresh_queue.finish(self.state, *args),
                                   profile=self.profile, runner=lambda *_: self.fail("should restore the cached fit"))
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["fit_review"]["score"], 100)
        refresh_worker.migrate(self.jobs, self.state, schema_only=True)
        self.assertFalse(refresh_queue.claim(self.state, "fit", 1))

    def test_isolated_hermes_adapter_recovers_only_the_safe_result(self):
        # Managed Hermes executes the adapter with -I, outside the installed dashboard package.
        path = fit_work.fit_hermes.__file__
        jd = fit_work.jd_input(self.doc)
        doc = dict(id=1, version=fit_work.stable_hash(jd), answer_facts={"A0001": self.answer}, **jd)
        result = response("private", self.profile, [doc])
        result["entries"][0]["reason"] = self.answer
        code = (
            "import json,runpy,sys; from pathlib import Path; "
            "module=runpy.run_path(sys.argv[1]); result,profile,doc=json.load(sys.stdin); "
            "directory=Path(sys.argv[2]); "
            "module['atomic_json'](directory/'private.json',module['private_receipt'](result,profile,doc)); "
            "print(json.dumps(module['recover'](directory,'private'),ensure_ascii=False))"
        )
        process = subprocess.run([sys.executable, "-I", "-c", code, path, self.directory.name],
                                 input=json.dumps([result, self.profile, doc]), text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn(self.answer, process.stdout)
        self.assertEqual(json.loads(process.stdout)["entries"][0]["decision"], "recommend")

    def test_changed_jd_ignores_old_answer_for_worker_and_rejects_old_form(self):
        from job_search import review_answers
        self.submit()
        with closing(collect_jobs.connect(self.jobs)) as db, db:
            doc = json.loads(db.execute("SELECT document FROM posts WHERE id=1").fetchone()[0])
            doc["lines"][0] = "변경된 SQL 실무 요건"
            collect_jobs.save_document(db, doc)
        with self.assertRaises(review_answers.AnswerChanged):
            self.submit()
        seen = []

        def evaluate(attempt, profile, docs):
            seen.extend(docs)
            return response(attempt, profile, docs)

        tasks = [dict(id=1, post_id=1)]
        fit_work.refresh_fit_tasks(self.jobs, self.state, tasks, lambda *_: None, profile=self.profile, runner=evaluate)
        self.assertNotIn("answer_facts", seen[0])
