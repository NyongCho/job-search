import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs as jobs, dashboard_store
from tests.test_collect_jobs import posting
from tests.test_job_fit import assessment, criterion


def response(attempt, profile, docs):
    entries = []
    for doc in docs:
        item = criterion(
            "필수",
            "required",
            "met",
            evidence="확인된 경력",
            quotes=[doc["lines"][0]],
            profile_ids=[next(iter(profile))],
        )
        entries.append(
            dict(
                id=doc["id"],
                version=doc["version"],
                decision="recommend",
                reason="필수 경험을 확인했습니다.",
                evidence_lines=[1],
                required_summary="필수 조건",
                assessment=assessment(item),
            )
        )
    return dict(
        entries=entries, calls=1, input_tokens=20, output_tokens=40, cache_read_tokens=0, cache_write_tokens=0
    )


class FitWorkTests(unittest.TestCase):
    def setUp(self):
        from job_search import fit_work

        self.fit = fit_work
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite"
        self.state = self.path.with_name("state.db")
        self.profile = {"P1": "필수 경험 확인"}
        self.saved = []
        self.collect(1, 2, 3, 4, 5)
        dashboard_store.init_state_db(self.path, self.state)

    def collect(self, *ids, suffix="", company_logo=""):
        posts = [
            posting(
                i,
                f"<p>직무{i} 필수 경험{suffix}</p><p>지원 마감 2099-01-01</p>"
                f'<a href="https://example.org/jobs/{i}">지원하러 가기</a>'
                + (f'<img alt="회사 로고" src="https://example.org/{company_logo}">' if company_logo else ""),
            )
            for i in ids
        ]
        if company_logo or suffix:
            for post in posts:
                post["modified_gmt"] = "2026-11-15T00:00:00"
        jobs.collect(self.path, lambda _: (posts, 1))

    def refresh(self, ids, runner=response, *, group="r", **args):
        tasks = [dict(id=f"{group}-{i}", post_id=i, version="catalog-version") for i in ids]

        def finish(task, state, message=""):
            self.saved.append((task["post_id"], state, message))

        self.fit.refresh_fit_tasks(
            self.path,
            self.state,
            tasks,
            finish,
            runner=runner,
            profile=args.pop("profile", self.profile),
            **args,
        )

    def legacy(self, post_id, status="met"):
        doc = jobs.get_posts(self.path, [post_id])[0]
        fit = assessment(criterion("필수", "required", status))
        if status == "unknown":
            fit["question"] = "필수 경험이 있나요?"
        entry = dict(
            id=post_id,
            fingerprint=doc["fingerprint"],
            decision="recommend",
            reason="경험 대조",
            evidence_lines=[1],
            assessment=fit,
            availability="open",
            deadline="2099-01-01",
            apply_url=doc["apply_url"],
            required_summary="필수 조건",
        )
        jobs.record_reviews(self.path, [entry], execution_id=f"prev-{post_id}")

    def test_default_submits_three_ready_jobs_and_sends_singletons_immediately(self):
        submitted = []

        def evaluate(attempt, profile, docs):
            submitted.append([doc["id"] for doc in docs])
            return response(attempt, profile, docs)

        with patch.dict("os.environ", clear=True):
            self.refresh([1, 2, 3, 4], evaluate)
            self.refresh([5], evaluate)
        self.assertEqual(submitted, [[1, 2, 3], [4], [5]])

    def test_numbered_long_jds_fit_the_actual_prompt_budget(self):
        ids = [101, 102, 103, 104]
        posts = [posting(i, "".join(f"<p>필수 {n}·{i}</p>" for n in range(140))) for i in ids]
        jobs.collect(self.path, lambda _: (posts, 1))
        docs = []
        for row in jobs.get_posts(self.path, ids):
            jd = self.fit.jd_input(row)
            docs.append(dict(id=row["id"], version=self.fit.stable_hash(jd), **jd))
        limit = len(self.fit.fit_hermes.prompt(self.profile, docs[:3])) - 1
        sent = []

        def evaluate(attempt, profile, batch):
            self.assertLessEqual(len(self.fit.fit_hermes.prompt(profile, batch)), limit)
            for doc in batch:
                self.assertEqual(len(doc["lines"]), 140)
            sent.append([doc["id"] for doc in batch])
            return response(attempt, profile, batch)

        with patch.dict("os.environ", {"FIT_INPUT_CHAR_LIMIT": str(limit)}):
            self.refresh(ids, evaluate, batch_size=3)
        self.assertEqual(sent, [[101, 102], [103, 104]])

    def test_validated_optional_and_work_gaps_generate_deliverable_questions(self):
        def gaps(attempt, profile, docs):
            result = response(attempt, profile, docs)
            for doc, entry in zip(docs, result["entries"]):
                item = entry["assessment"]["criteria"][0]
                item.update(
                    category="work" if doc["id"] == 1 else "preferred",
                    status="unknown",
                    label="배포 장애 원인 분석",
                    evidence="",
                    profile_ids=[],
                )
                entry["decision"] = "needs_review"
            return result

        self.collect(1, 2, suffix=" · 배포 장애 원인 분석")
        self.refresh([1, 2], gaps)
        digest = self.fit.scheduled_review(
            self.path,
            self.state,
            "unknown-role",
            ids=[1, 2],
            profile=self.profile,
            runner=lambda *_: self.fail("repeated question assessment"),
        )
        self.assertIn("확인 질문", digest)
        for doc in jobs.get_posts(self.path, [1, 2]):
            fit = doc["fit_review"]
            self.assertIsNone(fit["score"])
            self.assertEqual(doc["decision"], "needs_review")
            self.assertIn("배포 장애 원인 분석", fit["question"])
            self.assertEqual(len(fit["question"].splitlines()), 1)
            self.assertIn(fit["question"], digest)

    def test_existing_questions_and_non_applicant_holds_keep_their_original_routing(self):
        existing = "이전 업무에서 고객 교육을 맡았나요?"

        def gaps(attempt, profile, docs):
            result = response(attempt, profile, docs)
            for entry in result["entries"]:
                fit = entry["assessment"]
                fit["criteria"][0].update(
                    category="preferred", label="고객 교육", status="unknown", profile_ids=[]
                )
                if entry["id"] == 1:
                    fit["question"] = existing
                elif entry["id"] == 2:
                    fit["complete"] = False
                elif entry["id"] == 3:
                    fit["eligibility"] = "ineligible"
                elif entry["id"] == 4:
                    fit["eligibility"] = "unknown"
                elif entry["id"] == 5:
                    entry["decision"] = "exclude"
                elif entry["id"] == 6:
                    fit["criteria"][0]["label"] = "접수 마감일"
            return result

        self.collect(1, 2, 3, 4, 5, suffix=" · 고객 교육")
        self.collect(6, suffix=" · 접수 마감일")
        self.refresh(range(1, 7), gaps)
        docs = jobs.get_posts(self.path, range(1, 7))
        self.assertEqual([doc["fit_review"]["question"] for doc in docs], [existing, "", "", "", "", ""])

    def test_partial_batch_retries_only_missing_or_foreign_evidence(self):
        self.fit.migrate(self.path, self.state, profile=self.profile)

        def mixed(attempt, profile, docs):
            result = response(attempt, profile, docs)
            result["entries"][1]["assessment"]["criteria"][0]["quotes"] = ["다른 공고의 근거"]
            result["entries"].pop()
            return result

        self.refresh([1, 2, 3], mixed, batch_size=3)
        self.assertEqual({i: s for i, s, _ in self.saved}, {1: "done", 2: "failed", 3: "failed"})
        received = []

        def retry(a, p, docs):
            received.extend(d["id"] for d in docs)
            return response(a, p, docs)

        self.refresh([1, 2, 3], retry, group="second", batch_size=3)
        self.assertEqual(received, [2, 3])
        self.assertEqual(
            [doc["fit_review"]["score"] for doc in jobs.get_posts(self.path, [1, 2, 3])], [100] * 3
        )

    def test_clicked_again_or_joined_cron_sends_once_never_refits(self):
        self.legacy(1, "unmet")
        self.legacy(2, "unknown")
        self.fit.migrate(self.path, self.state, profile=self.profile)
        called = []

        def run(a, p, docs):
            called.extend(d["id"] for d in docs)
            return response(a, p, docs)

        self.refresh(range(1, 6), run, batch_size=5)
        self.refresh(range(1, 6), run, group="again", batch_size=5)
        self.assertEqual(called, [3, 4, 5])
        self.assertEqual(jobs.get_posts(self.path, [1])[0]["fit_review"]["score"], 0)
        self.assertEqual(jobs.get_posts(self.path, [2])[0]["fit_review"]["score"], None)
        digest = self.fit.scheduled_review(self.path, self.state, "cron", profile=self.profile, runner=run)
        self.assertIn("https://example.org/jobs/3", digest)
        self.assertEqual(
            self.fit.scheduled_review(self.path, self.state, "cron", profile=self.profile, runner=run), digest
        )
        self.assertEqual(
            self.fit.scheduled_review(self.path, self.state, "next", profile=self.profile, runner=run),
            "[SILENT]",
        )
        self.assertEqual(called, [3, 4, 5])
        measured = self.fit.usage(self.path, ["r-3", "r-4", "r-5"])
        self.assertEqual(
            [(r["calls"], r["input_tokens"], r["submitted"], r["successful"]) for r in measured],
            [(1, 20, 3, 3)],
        )
        self.assertFalse(any(r["owned"] for r in self.fit.usage(self.path, ["again-3"])))

    def test_s0_is_frozen_and_logo_only_recollection_preserves_read_and_delivery(self):
        self.legacy(1)
        self.fit.migrate(self.path, self.state, profile=self.profile)
        original = jobs.get_posts(self.path, [1])[0]["fit_review"]["reviewed_at"]
        self.collect(1, company_logo="new-logo.png")
        self.refresh([1], lambda *_: self.fail("metadata change triggered model"))
        self.assertEqual(jobs.get_posts(self.path, [1])[0]["fit_review"]["reviewed_at"], original)
        called = []

        def run(a, p, docs):
            called.extend(d["id"] for d in docs)
            return response(a, p, docs)

        self.refresh([1], run, profile={"P1": "추가 답변"}, group="new-profile")
        self.collect(1, suffix="경력 5년")
        self.refresh([1], run, profile={"P1": "추가 답변"}, group="new-jd")
        self.assertEqual(called, [1, 1])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("select count(*) from outbox").fetchone()[0], 1)

    def test_profile_only_record_never_rated_using_application_notes(self):
        self.fit.migrate(self.path, self.state, profile=self.profile)
        with sqlite3.connect(self.state) as db:
            db.execute(
                "INSERT INTO profile_posts VALUES(-1,'applications/h',?,'2026-11-01','2026-11-01')",
                (json.dumps({"lines": ["채용 절차 서류 합격"]}),),
            )
        self.refresh([-1], lambda *_: self.fail("profile history reached model"))
        with sqlite3.connect(self.state) as db:
            fit = self.fit.profile_fit(db, -1)
        self.assertIsNone(fit["score"])
        self.assertIn("원문", fit["hold_reason"])

    def test_overlapping_workers_submit_one_id_once(self):
        self.fit.migrate(self.path, self.state, profile=self.profile)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow(a, p, docs):
            calls.append(a)
            entered.set()
            self.assertTrue(release.wait(8))
            return response(a, p, docs)

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.refresh, [1], slow)
            self.assertTrue(entered.wait(8))
            second = pool.submit(self.refresh, [1], slow, group="join")
            joined = []
            deadline = time.monotonic() + 2
            while not joined and time.monotonic() < deadline:
                joined = self.fit.usage(self.path, ["join-1"])
                if not joined:
                    time.sleep(0.01)
            release.set()
            self.assertTrue(joined)
            self.assertFalse(joined[0]["owned"])
            first.result()
            second.result()
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(s == "done" for _, s, _ in self.saved))

    def test_changed_input_during_inference_never_publishes_old_score(self):
        self.fit.migrate(self.path, self.state, profile=self.profile)

        def stale(a, p, docs):
            self.collect(1, suffix="5년")
            return response(a, p, docs)

        self.refresh([1], stale)
        self.assertIsNone(jobs.get_posts(self.path, [1])[0]["fit_review"])

    def test_ambiguous_submission_is_not_automatically_resubmitted(self):
        self.fit.migrate(self.path, self.state, profile=self.profile)

        def disappeared(attempt, profile, docs):
            marker = Path(str(self.path) + ".fit-runs") / (attempt + ".submitted")
            self.fit.fit_hermes.atomic_json(marker, {"session_id": attempt})
            raise OSError("submission status unavailable")

        self.refresh([1], disappeared)
        self.refresh([1], lambda *_: self.fail("ambiguous attempt repeated"), group="retry")
        self.assertEqual(self.saved[-1], (1, "failed", "평가 실행 확인 필요"))

    def test_restart_automatically_evaluates_claim_that_never_reached_hermes(self):
        class WorkerStopped(BaseException):
            pass

        def stop_after_claim(attempt, profile, docs):
            raise WorkerStopped()

        with self.assertRaises(WorkerStopped):
            self.refresh([1, 2], stop_after_claim, batch_size=2)
        submitted = []

        def run(attempt, profile, docs):
            submitted.append([doc["id"] for doc in docs])
            return response(attempt, profile, docs)

        self.refresh([1, 2], run, batch_size=2, group="restart")
        self.refresh([1, 2], run, batch_size=2, group="again")
        self.assertEqual(submitted, [[1, 2]])
        self.assertEqual(self.saved[:2], [(1, "done", "평가 완료"), (2, "done", "평가 완료")])
        attempts = self.fit.usage(self.path, ["restart-1", "restart-2"])
        self.assertEqual([(a["calls"], a["submitted"]) for a in attempts], [(0, 0), (1, 2)])

    def test_interrupted_after_submitting_recovers_by_persisted_session_id(self):
        self.fit.migrate(self.path, self.state, profile=self.profile)

        class SimulatedExit(BaseException):
            pass

        def interrupted(attempt, profile, docs):
            self.fit.fit_hermes.atomic_json(
                Path(str(self.path) + ".fit-runs") / (attempt + ".json"), response(attempt, profile, docs)
            )
            raise SimulatedExit()

        with self.assertRaises(SimulatedExit):
            self.refresh([1], interrupted)
        self.refresh([1], lambda *_: self.fail("completed attempt resubmitted"), group="recovered")
        self.assertEqual(jobs.get_posts(self.path, [1])[0]["fit_review"]["score"], 100)

    def test_profile_record_with_verified_jd_can_replace_hold(self):
        self.fit.migrate(self.path, self.state, profile=self.profile)
        doc = {"id": -1, "title": "기존 지원", "lines": ["지원 메모"]}
        with sqlite3.connect(self.state) as db:
            db.execute(
                "INSERT INTO profile_posts VALUES(-1,'applications/h',?,'2026-11-01','2026-11-01')",
                (json.dumps(doc),),
            )
        self.refresh([-1])
        with sqlite3.connect(self.state) as db:
            doc["jd"] = {
                "title": "recovered",
                "url": "https://e.test/job",
                "lines": ["실제 필수 요건"],
                "jd_source": {"source": "official", "original_id": "abc", "url": "https://e.test/job"},
            }
            db.execute("UPDATE profile_posts SET document=?", (json.dumps(doc),))
        self.refresh([-1], group="updated")
        with sqlite3.connect(self.state) as db:
            self.assertEqual(self.fit.profile_fit(db, -1)["score"], 100)
            self.assertEqual(db.execute("select count(*) from fit_attempts").fetchone()[0], 1)

    def test_memory_and_explicit_local_career_document_change_profile_version(self):
        home = Path(self.temp.name) / "hermes"
        (home / "memories").mkdir(parents=True)
        cv = home / "career.md"
        cv.write_text("# 경력\n장애 대응 3년")
        (home / "memories" / "USER.md").write_text("희망 직무: 엔지니어\n")
        (home / "memories" / "MEMORY.md").write_text(f"경력 문서 [{cv.name}]({cv})")
        with patch.dict("os.environ", {"HERMES_HOME": str(home)}):
            base = self.fit.read_profile()
            cv.write_text("# 경력\n장애 대응 4년")
            updated = self.fit.read_profile()
            self.assertIn("장애 대응 4년", updated.values())
            self.assertNotEqual(base, updated)
            self.assertEqual(updated, self.fit.read_profile())

    def test_profile_keeps_career_section_facts_across_paragraphs(self):
        home = Path(self.temp.name) / "hermes"
        memory = home / "memories" / "USER.md"
        memory.parent.mkdir(parents=True)
        profile_versions = []
        with patch.dict("os.environ", {"HERMES_HOME": str(home)}):
            for years in (2, 5):
                memory.write_text(f"# 경력\n\n## 운영\n\nPostgreSQL DBA {years}년\n"
                                  "\n# 식사\n\n수요일 메뉴\n§\n개인 일정")
                profile = self.fit.read_profile()
                self.assertIn(f"PostgreSQL DBA {years}년", profile.values())
                self.assertNotIn("수요일 메뉴", profile.values())
                self.assertNotIn("개인 일정", profile.values())
                profile_versions.append(self.fit.stable_hash(profile))
        self.assertNotEqual(*profile_versions)

    def test_deadline_change_reroutes_cached_result_without_model(self):
        self.legacy(1)
        def initial(attempt, profile, docs):
            result = response(attempt, profile, docs)
            for entry in result["entries"]:
                if entry["id"] == 3:
                    entry["decision"] = "exclude"
            return result
        self.refresh([1, 2, 3], initial)

        def counts():
            result = dashboard_store.list_jobs(self.path, self.state, view="expired")
            return result["counts"]["recommended"], result["counts"]["expired"]

        self.assertEqual(counts(), (2, 0))
        for day, expected in [("2001-01-01", (0, 2)), ("2099-01-01", (2, 0))]:
            posts = [posting(i, f"<p>직무{i} 필수 경험</p><p>지원 마감 {day}</p>"
                                f'<a href="https://example.org/jobs/{i}">지원하러 가기</a>') for i in (1, 2, 3)]
            if day == "2099-01-01":
                from job_search.refresh_job_availability import refresh
                reopened = refresh(self.path, self.state, fetch_page=lambda _: (posts[:2], 1))
                self.assertEqual((reopened["requested"], reopened["checked"]), (2, 2))
                self.assertEqual(counts(), expected)
            jobs.collect(self.path, lambda _: (posts, 1))
            self.refresh([1, 2, 3], lambda *_: self.fail("deadline change triggered inference"), group=day)
            self.assertEqual(counts(), expected)
            ids = dashboard_store.refresh_targets(self.path, self.state, view="expired")
            self.assertEqual({post_id for post_id, _ in ids}, {1, 2} if expected[1] else set())
            self.assertEqual(jobs.get_posts(self.path, [3])[0]["decision"], "exclude")

    def test_backfill_uses_only_pending_manifest_ids_and_completion_skips_collection(self):
        import fcntl

        self.fit.migrate(self.path, self.state, profile=self.profile)
        lock = self.path.parent / "backfill.lock"
        state = self.path.parent / "backfill-status.json"
        with lock.open("w") as active:
            fcntl.flock(active, fcntl.LOCK_EX)
            state.write_text(json.dumps({"state": "running", "batch_ids": [3, 5]}))
            ids = jobs.scheduled_batch(self.path)
            called = []

            def run(a, p, docs):
                called.extend(d["id"] for d in docs)
                return response(a, p, docs)

            digest = self.fit.scheduled_review(
                self.path, self.state, "backfill", ids=ids, profile=self.profile, runner=run
            )
            self.assertEqual(called, [3, 5])
            self.assertIn("https://example.org/jobs/3", digest)
            self.assertNotIn("https://example.org/jobs/1", digest)
            state.write_text(json.dumps({"state": "review_complete"}))
            self.assertEqual(jobs.scheduled_batch(self.path), [])
        self.assertIsNone(jobs.scheduled_batch(self.path))

    def test_queued_versions_for_same_post_share_one_submission_and_finish_both(self):
        import multiprocessing

        task_ids = [17, 21]
        finished = multiprocessing.Queue()

        def work():
            def one(attempt, profile, docs):
                if len(docs) != 1:
                    raise AssertionError("duplicate ID in model batch")
                return response(attempt, profile, docs)

            self.fit.refresh_fit_tasks(
                self.path,
                self.state,
                [dict(id=i, post_id=1, version=str(i)) for i in task_ids],
                lambda t, s, m: finished.put((t["id"], s)),
                profile=self.profile,
                runner=one,
                batch_size=2,
            )

        child = multiprocessing.get_context("fork").Process(target=work)
        child.start()
        child.join(2)
        try:
            self.assertFalse(child.is_alive(), "worker waited on its own submission")
            self.assertEqual([finished.get(timeout=1) for _ in task_ids], [(17, "done"), (21, "done")])
        finally:
            if child.is_alive():
                child.terminate()
            child.join()

    def test_confirmed_preflight_failure_can_retry_without_guessing_provider_state(self):
        with patch.dict("os.environ", {"HERMES_PYTHON": str(self.path.parent / "no-such-python")}):
            self.refresh([1], runner=None)
        failed, = self.fit.usage(self.path, ["r-1"])
        self.assertEqual((failed["submitted"], failed["failed"]), (0, 1))
        self.assertEqual([failed[k] for k in self.fit.COUNTERS], [0] * len(self.fit.COUNTERS))
        self.refresh([1], group="retry-after-fix")
        self.assertEqual(self.saved[-1][:2], (1, "done"))

    def test_old_migrated_score_is_never_retroactively_attached_to_a_new_answer(self):
        self.legacy(1)
        self.fit.migrate(self.path, self.state, profile=self.profile)
        received = []

        def changed(a, p, docs):
            received.extend(d["id"] for d in docs)
            return response(a, p, docs)

        self.refresh([1], runner=changed, profile={"P2": "새로 확인한 경력"})
        self.assertEqual(received, [1])

    def test_unrelated_memory_files_are_not_used_and_continuations_are_kept(self):
        home = Path(self.temp.name) / "hermes"
        (home / "memories").mkdir(parents=True)
        unrelated = home / "other.md"
        unrelated.write_text("공고와 무관한 개인 기록")
        (home / "memories" / "USER.md").write_text("직무 선호 답변\n엔진 개발\n\n")
        (home / "memories" / "MEMORY.md").write_text(f"참고 문서 {unrelated}")
        with patch.dict("os.environ", {"HERMES_HOME": str(home)}):
            values = self.fit.read_profile().values()
            self.assertIn("엔진 개발", values)
            self.assertNotIn("공고와 무관한 개인 기록", values)

    def test_unconfirmed_application_retries_without_rerating_or_delivering_a_source_link(self):
        missing = posting(1, "<p>필수 조건</p><p>지원 마감 2099-01-01</p>")
        missing["modified_gmt"] = "2026-11-16T00:00:00"
        jobs.collect(self.path, lambda _: ([missing], 1))
        self.refresh([1])
        checks = []

        def verify(docs):
            checks.append([d["id"] for d in docs])
            return {} if len(checks) == 1 else {1: ("open", "https://employer.test/apply", "2099-01-01")}

        opts = dict(
            ids=[1],
            profile=self.profile,
            runner=lambda *_: self.fail("uncertain opening triggered model"),
            verifier=verify,
        )
        self.assertEqual(self.fit.scheduled_review(self.path, self.state, "first", **opts), "[SILENT]")
        digest = self.fit.scheduled_review(self.path, self.state, "second", **opts)
        self.assertIn("지원 https://employer.test/apply", digest)
        self.assertNotIn("지원 https://inthiswork.com/", digest)
        self.assertEqual(checks, [[1], [1]])

    def test_verified_no_deadline_link_is_sent_and_closed_candidate_is_retired(self):
        urls = {1: "https://job-boards.greenhouse.io/firm/jobs/91", 2: "https://firm.invalid/2",
                3: "https://deleted.invalid/deleted"}
        fresh = [posting(i, f'<p>필수 {i}</p><a href="{url}">지원하러 가기</a>') for i, url in urls.items()]
        jobs.collect(self.path, lambda _: (fresh, 1))
        fresh[1]["categories"].append(jobs.CLOSED)
        self.refresh(urls.keys())

        def employer(url, **_):
            self.assertEqual(url, "https://boards-api.greenhouse.io/v1/boards/firm/jobs/91?content=true")
            body = {"id": 91, "title": "엔지니어", "company_name": "회사", "content": "<p>필수 1</p>",
                    "absolute_url": urls[1]}
            return url, json.dumps(body).encode(), "application/json"

        verify = lambda docs: self.fit.verify_openings(docs, fetch_page=lambda url: (fresh, 1), fetch=employer)
        digest = self.fit.scheduled_review(
            self.path, self.state, "live", ids=list(urls), profile=self.profile, runner=response, verifier=verify
        )
        self.assertIn("지원 " + urls[1], digest)
        self.assertNotIn(urls[2], digest)
        self.assertNotIn(urls[3], digest)
        self.assertEqual(jobs.get_posts(self.path, [2])[0]["decision"], "exclude")
        self.assertEqual(jobs.get_posts(self.path, [3])[0]["decision"], "needs_review")
        with sqlite3.connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT execution_id FROM fit_delivery WHERE post_id=3").fetchone()[0])

    def test_relative_link_from_user_memory_updates_evidence(self):
        memory = Path(self.temp.name) / "memories"
        memory.mkdir()
        (memory / "USER.md").write_text("경력 자료 [이력서](career.md)")
        (memory / "career.md").write_text("운영 5년")
        with patch.dict("os.environ", {"HERMES_HOME": self.temp.name}):
            self.assertIn("운영 5년", self.fit.read_profile().values())

    def test_failed_first_hundred_never_starve_later_new_jobs(self):
        self.collect(*range(6, 102))
        called = []

        def run(a, p, docs):
            called.extend(doc["id"] for doc in docs)
            return response(a, p, [doc for doc in docs if doc["id"] == 101])

        with patch.dict("os.environ", {"FIT_BATCH_SIZE": "5"}):
            for execution in ("one", "two"):
                self.fit.scheduled_review(self.path, self.state, execution, profile=self.profile, runner=run)
        self.assertEqual(called[:101], list(range(1, 102)))
        self.assertEqual(jobs.get_posts(self.path, [101])[0]["fit_review"]["score"], 100)

    def test_recheck_cached_profile_after_waiting_for_a_write_lock(self):
        self.refresh([1])
        active = [self.profile]
        next_profile = {"P1": "새로 확인한 답변"}
        original = self.fit._db

        def db(path, *args):
            connection = original(path, *args)
            if path == self.path:
                # The writer may wait for another transaction before it can claim or publish a cache hit.
                def after_lock_wait(sql):
                    if sql == "BEGIN IMMEDIATE":
                        active[0] = next_profile
                connection.set_trace_callback(after_lock_wait)
            return connection

        submitted = []
        def evaluate(attempt, profile, docs):
            submitted.append(profile)
            return response(attempt, profile, docs)

        with patch.object(self.fit, "_db", db), patch.object(self.fit, "read_profile", lambda: active[0]):
            self.fit._batch(self.path, self.state, [{"id": "waiting", "post_id": 1}], active[0], False,
                            evaluate, 1, Path(str(self.path) + ".fit-runs"), lambda *args: None)
        self.assertEqual(submitted, [next_profile])

    def test_old_profile_response_cannot_replace_newer_committed_score(self):
        entered, release = threading.Event(), threading.Event()

        def old(a, p, docs):
            entered.set()
            self.assertTrue(release.wait(5))
            return response(a, p, docs)

        def current(a, p, docs):
            result = response(a, p, docs)
            result["entries"][0]["assessment"]["criteria"][0]["status"] = "unmet"
            return result

        with ThreadPoolExecutor(2) as pool:
            slow = pool.submit(self.refresh, [1], old, concurrency=2)
            self.assertTrue(entered.wait(5))
            self.refresh([1], current, group="new", profile={"P2": "새 답변"}, concurrency=2)
            release.set()
            slow.result()
        self.assertEqual(jobs.get_posts(self.path, [1])[0]["fit_review"]["score"], 0)
