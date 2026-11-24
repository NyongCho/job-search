"""Synthetic review cases at the collector and scheduled-delivery boundary."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from job_search import collect_jobs, fit_work
from tests.test_collect_jobs import posting
from tests.test_fit_work import response
from tests.test_job_fit import assessment, criterion


class ReviewDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.sqlite"
        self.state = self.jobs.with_name("state.sqlite")
        content = ('<p>야간 데이터 운영 경험이 필요합니다.</p><p>마감 2099-03-01</p>'
                   '<a href="https://example.org/apply">지원하러가기</a>')
        collect_jobs.collect(self.jobs, lambda _: ([posting(1, content)], 1))

    def submit_question(self, question, owner, **options):
        doc, = collect_jobs.get_posts(self.jobs, [1])
        fit = assessment(criterion("야간 운영", "required", "unknown"), question=question,
                         question_owner=owner)
        return collect_jobs.record_reviews(self.jobs, [{
            "id": 1, "decision": "needs_review", "assessment": fit, "reason": "야간 운영 조건",
            "fingerprint": doc["fingerprint"], "availability": "open", "evidence_lines": [1]
        }], execution_id="first", **options)

    def test_employer_condition_is_not_emitted_in_scheduled_message(self):
        digest = self.submit_question("기업에서 야간 근무 경험을 인정하나요?", "employer")
        self.assertEqual(digest, "[SILENT]")
        stored, = collect_jobs.get_posts(self.jobs, [1])
        self.assertEqual(stored["fit_review"]["question_owner"], "employer")

    def test_applicant_question_is_delivered_and_ownership_is_preserved(self):
        digest = self.submit_question("야간 데이터 운영 업무를 직접 담당한 경험이 있나요?", "applicant")
        self.assertIn("직접 담당한 경험", digest)
        stored, = collect_jobs.get_posts(self.jobs, [1])
        self.assertEqual(stored["fit_review"]["question_owner"], "applicant")

    def test_manual_review_uses_same_personal_scope_as_scheduled_notices(self):
        from job_search import dashboard_store
        dashboard_store.init_state_db(self.jobs, self.state)
        dashboard_store.set_status(self.jobs, self.state, 1, "applied")
        self.assertEqual(self.submit_question("야간 데이터 운영을 담당한 경험이 있나요?", "applicant",
                                             state_path=self.state), "[SILENT]")

    def test_closed_or_applied_jobs_do_not_send_questions(self):
        from job_search import dashboard_store
        dashboard_store.init_state_db(self.jobs, self.state)
        dashboard_store.set_status(self.jobs, self.state, 1, "applied")
        def evaluate(attempt, profile, docs):
            result = response(attempt, profile, docs)
            for entry in result["entries"]:
                item = entry["assessment"]["criteria"][0]
                item.update(status="unknown", label="야간 데이터 운영", profile_ids=[])
                entry["assessment"].update(question_owner="applicant", question="야간 운영을 담당한 경험이 있나요?")
            return result
        digest = fit_work.scheduled_review(self.jobs, self.state, "applied", runner=evaluate, profile={"P1": "가상 근거"})
        self.assertEqual(digest, "[SILENT]")
        dashboard_store.set_status(self.jobs, self.state, 1, "unmarked")
        with closing(collect_jobs.connect(self.jobs)) as db, db:
            doc = json.loads(db.execute("SELECT document FROM posts WHERE id=1").fetchone()[0])
            doc["lines"].append("접수 마감 2000-01-01")
            doc["lines"] = [line for line in doc["lines"] if "2099" not in line]
            collect_jobs.save_document(db, doc)
        self.assertEqual(self.submit_question("야간 데이터 운영 업무 경험이 있나요?", "applicant"), "[SILENT]")

    def test_recovered_poster_reevaluates_changed_evidence_and_preserves_delivery(self):
        self.submit_question("야간 데이터 운영 업무를 담당한 경험이 있나요?", "applicant")
        collect_jobs.reconcile_outbox(self.jobs, {"first": "delivered"}, "receipt")
        profile = {"P1": "가상 직무 경험"}
        fit_work.migrate(self.jobs, self.state, profile=profile)
        with closing(collect_jobs.connect(self.jobs)) as db, db:
            row = db.execute("SELECT document FROM posts WHERE id=1").fetchone()
            doc = json.loads(row["document"])
            doc["lines"].append("데이터 운영과 배포를 수행합니다.")
            doc["poster_evidence"] = {"status": "ready"}
            collect_jobs.save_document(db, doc)
            self.assertIsNone(db.execute("SELECT input_key FROM fit_current WHERE post_id=1").fetchone())
            self.assertIsNotNone(db.execute("SELECT execution_id FROM fit_delivery WHERE post_id=1").fetchone()[0])
        calls = []
        def evaluate(attempt, facts, documents):
            calls.extend(doc["id"] for doc in documents)
            return response(attempt, facts, documents)
        options = {"profile": profile, "runner": evaluate}
        fit_work.refresh_fit_tasks(self.jobs, self.state, [{"id": "poster", "post_id": 1}],
                                   lambda *_: None, **options)
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["fit_review"]["score"], 100)
        with closing(collect_jobs.connect(self.jobs)) as db, db:
            collect_jobs.save_document(db, doc)
        self.assertEqual(fit_work.scheduled_review(self.jobs, self.state, "repeat", **options), "[SILENT]")
        self.assertEqual(calls, [1])
        with closing(collect_jobs.connect(self.jobs)) as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT id,delivered FROM outbox")], [("first", 1)])
        collect_jobs.collect(self.jobs, lambda _: ([posting(2, '<p>야간 데이터 운영</p><p>마감 2099-03-01</p>'
            '<a href="https://example.org/job">지원하러가기</a>')], 1))
        self.assertIn("100.0점", fit_work.scheduled_review(self.jobs, self.state, "new", **options))
        self.assertEqual(calls, [1, 2])
