import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs, fit_work
from tests.test_collect_jobs import posting
from tests.test_fit_work import response
from tests.test_job_fit import assessment, criterion


class ManualFitCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.jobs = Path(self.directory.name) / "jobs.db"
        self.state = self.jobs.with_name("state.db")
        self.profile = {"P1": "합성 경력"}
        self.doc = posting(1, '<p>업무 경험</p><p>지원 마감 2099-01-01</p>'
                              '<a href="https://example.org/jobs/1">지원하러 가기</a>')
        collect_jobs.collect(self.jobs, lambda _: ([self.doc], 1))
        fit_work.migrate(self.jobs, self.state, profile=self.profile)

    def refresh(self, runner=response, profile=None):
        saved = []
        fit_work.refresh_fit_tasks(self.jobs, self.state, [dict(id="test", post_id=1)],
                                   lambda task, state, message: saved.append(state), runner=runner,
                                   profile=self.profile if profile is None else profile)
        return saved

    def manual(self, scored=True):
        doc = collect_jobs.get_posts(self.jobs, [1])[0]
        entry = dict(id=1, decision="exclude", reason="수동 검토 결과", fingerprint=doc["fingerprint"], evidence_lines=[1])
        if scored:
            entry.update(availability="open", assessment=assessment(criterion("직무 경험", "work", "unmet")))
        collect_jobs.record_reviews(self.jobs, [entry], execution_id="manual")

    def test_assessed_manual_review_wins_over_inflight_and_records_model_usage(self):
        def race(attempt, profile, docs):
            self.manual()
            return response(attempt, profile, docs)
        self.assertEqual(self.refresh(race), ["skipped"])
        self.refresh(lambda *_: self.fail("현재 입력의 수동 검토를 다시 평가했습니다."))
        doc = collect_jobs.get_posts(self.jobs, [1])[0]
        self.assertEqual((doc["decision"], doc["reason"], doc["fit_review"]["score"]), ("exclude", "수동 검토 결과", 0))
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertEqual(db.execute("SELECT submitted,successful FROM fit_attempts").fetchall(), [(1, 1)])

    def test_unscored_manual_review_invalidates_previously_cached_assessment_only(self):
        self.refresh()
        self.manual(scored=False)
        self.assertIsNone(collect_jobs.get_posts(self.jobs, [1])[0]["fit_review"])
        called = []

        def evaluate(attempt, profile, docs):
            called.extend(doc["id"] for doc in docs)
            return response(attempt, profile, docs)

        self.refresh(evaluate)
        self.assertEqual(called, [1])

    def test_worker_for_previous_jd_cannot_change_a_new_manual_review(self):
        def race(attempt, profile, docs):
            with closing(collect_jobs.connect(self.jobs)) as db, db:
                doc = json.loads(db.execute("SELECT document FROM posts WHERE id=1").fetchone()[0])
                doc["lines"][0] = "변경된 직무의 요건"
                collect_jobs.save_document(db, doc)
            with patch.object(fit_work, "read_profile", return_value=self.profile):
                self.manual()
            return response(attempt, profile, docs)

        self.assertEqual(self.refresh(race), ["skipped"])
        self.refresh(lambda *_: self.fail("새 JD의 검토를 재사용해야 합니다."))
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["fit_review"]["score"], 0)
        self.refresh(profile={"P1": "새 프로필"})
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["fit_review"]["score"], 100)
