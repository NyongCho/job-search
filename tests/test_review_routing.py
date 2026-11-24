import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs, dashboard_store as store
from job_search.review_routing import route_review
from tests.test_collect_jobs import posting
from tests.test_dashboard_store import collect_fit_examples


class ReviewRoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobs = Path(tmp.name) / "jobs.sqlite"
        self.state = Path(tmp.name) / "state.sqlite"
        collect_fit_examples(self.jobs)
        store.init_state_db(self.jobs, self.state)

    def detail(self, post_id=5):
        return store.get_job(self.jobs, self.state, post_id)

    def items(self, view):
        return {item["id"] for item in store.list_jobs(self.jobs, self.state, view=view)["items"]}

    def change_fit(self, **values):
        with sqlite3.connect(self.jobs) as db:
            old = json.loads(db.execute("SELECT fit_review FROM posts WHERE id=5").fetchone()[0])
            db.execute("UPDATE posts SET fit_review=? WHERE id=5", (json.dumps({**old, **values}),))

    def test_employer_and_system_questions_do_not_reach_the_recommended_inbox(self):
        for question in ("회사에서 장애 대응 경력도 인정하나요?", "지원 마감일은 언제인가요?",
                         "재택 근무를 허용하나요?", "전일제 근무가 가능한 포지션인가요?",
                         "재택 근무가 가능한가요?", "인턴 경력을 포함하나요?",
                         "Is your company hiring remote workers?", "비행기를 직접 조종한 경험이 있나요?",
                         "경험이 있나요?", "자세한 조건을 알려주세요"):
            with self.subTest(question=question):
                self.change_fit(question=question)
                self.assertNotIn(5, self.items("needs_review"))
                self.assertNotIn(5, self.items("recommended"))
                job = self.detail()
                self.assertEqual((job["review_route"], job["review_questions"]), ("hold", []))
                self.assertTrue(job["review_reason"])
                self.assertEqual(job["fit"]["question"], "")
                self.assertIn(5, self.items("processing_hold"))
                self.assertIn(5, {n for n, _ in store.refresh_targets(
                    self.jobs, self.state, view="processing_hold")})

    def test_question_references_identify_who_can_resolve_the_hold(self):
        for question, reason in (
            ("마감일을 언제로 확인하셨나요?", "접수"),
            ("공고 원문을 확인하셨나요?", "본문"),
            ("채용 포스터 이미지를 읽을 수 있나요?", "이미지"),
            ("전일제 근무가 이 직무의 필수인가요?", "기업"),
            ("회사가 계약직 경력도 받아주나요?", "기업"),
        ):
            with self.subTest(question=question):
                self.change_fit(question=question)
                self.assertEqual(self.detail()["review_questions"], [])
                self.assertIn(reason, self.detail()["review_reason"])
        self.change_fit(question="이전 회사에서 운영 장애에 직접 대응했나요?")
        self.assertEqual(self.detail()["review_route"], "question")
        stored = self.detail()["fit"]
        for owner, reason in (("employer", "기업"), ("system", "평가")):
            review = route_review({"lines": ["직무 경험 요건"]}, "needs_review",
                                  {**stored, "question": "", "question_owner": owner}, {})
            self.assertIn(reason, review["reason"])

    def test_question_moves_through_hold_and_expired_and_preserves_personal_state(self):
        store.set_status(self.jobs, self.state, 5, "interested")
        self.assertEqual(self.detail()["review_questions"], ["운영 장애에 직접 대응했나요?"])
        self.change_fit(availability="unknown")
        self.assertIn("접수", self.detail()["review_reason"])
        self.assertNotIn(5, self.items("needs_review"))
        self.assertIn(5, self.items("processing_hold"))
        store.set_deadline(self.jobs, self.state, 5, {"mode": "manual", "kind": "date", "date": "2000-01-01"})
        self.assertIn(5, self.items("expired"))
        self.assertNotIn(5, self.items("processing_hold"))
        self.assertEqual((self.detail()["review_route"], self.detail()["status"]), (None, "interested"))
        store.set_status(self.jobs, self.state, 5, "applied")
        self.assertNotIn(5, self.items("expired"))
        self.assertIn(5, self.items("applied"))
        self.assertEqual(self.detail()["review_questions"], [])

    def test_new_manual_window_can_resolve_an_unknown_opening_without_inference(self):
        self.change_fit(availability="unknown", apply_url="https://example.org/jobs/5")
        self.assertEqual(self.detail()["review_route"], "hold")
        store.set_deadline(self.jobs, self.state, 5, {"mode": "manual", "kind": "date", "date": "2099-05-21"})
        self.assertEqual(self.detail()["review_route"], "question")
        self.assertEqual(self.detail()["fit"]["availability"], "open")
        store.set_deadline(self.jobs, self.state, 5, {"mode": "auto"})
        self.assertEqual(self.detail()["review_route"], "hold")

    def test_personal_refresh_only_parses_deadlines_of_marked_jobs(self):
        store.set_status(self.jobs, self.state, 5, "interested")
        with patch.object(store, "availability", wraps=store.availability) as read_deadline:
            targets = store.refresh_targets(self.jobs, self.state, view="interested")
            self.assertEqual([i for i, _ in targets], [5])
            self.assertEqual(read_deadline.call_count, 1)

    def test_closed_system_review_without_a_recommendation_group_moves_to_expired(self):
        with sqlite3.connect(self.jobs) as db:
            db.execute("""UPDATE posts SET decision='exclude',fit_review=json_set(fit_review,
                          '$.availability','closed','$.assessment_decision','needs_review',
                          '$.question_owner','employer') WHERE id=5""")
        self.assertTrue(self.detail()["is_expired"])
        self.assertIn(5, self.items("expired"))
        self.assertNotIn(5, self.items("needs_review"))


    def test_closed_v1_without_original_decision_only_recovers_a_supported_question(self):
        self.change_fit(availability="closed")
        with sqlite3.connect(self.jobs) as db:
            db.execute("""UPDATE posts SET decision='exclude',
                          fit_review=json_remove(fit_review,'$.assessment_decision') WHERE id=5""")
        self.assertIn(5, self.items("expired"))
        self.change_fit(question="기업에서 전일제 근무를 허용하나요?")
        self.assertNotIn(5, self.items("expired"))
        self.assertNotIn(5, self.items("needs_review"))

    def test_old_rows_and_unassessed_new_rows_route_immediately_using_current_jd(self):
        content = ('<p>전일제 근무가 필수입니다.</p><p>접수 마감 2099-05-21</p>'
                   '<a href="https://example.org/jobs/full-time">지원하러 가기</a>')
        collect_jobs.collect(self.jobs, lambda _: ([posting(i, content) for i in range(10, 14)], 1))
        for i, reason, lines in ((10, "전일제로 근무할 수 있나요?", [1]),
                                 (11, "전일제로 근무할 수 있나요?", []),
                                 (12, "지원자의 경험을 기업에서 인정하나요?", [1])):
            with sqlite3.connect(self.jobs) as db:
                db.execute("UPDATE posts SET decision='needs_review',reason=?,evidence_lines=? WHERE id=?",
                           (reason, json.dumps(lines), i))
        self.assertEqual(self.detail(10)["review_questions"], ["전일제로 근무할 수 있나요?"])
        self.assertEqual(self.items("needs_review"), {5, 10})
        self.assertTrue({11, 12, 13} <= self.items("processing_hold"))
        self.assertIn("평가 대기", self.detail(13)["review_reason"])
        self.assertIn("기업", self.detail(12)["review_reason"])
        self.assertEqual(self.items("recommended") & {10, 11, 12, 13}, {10})

    def test_empty_or_unread_poster_evidence_takes_precedence_over_any_user_question(self):
        collect_jobs.collect(self.jobs, lambda _: ([posting(10, '<p>지원하러 가기</p>'),
            posting(11, '<p>설명은 공고 이미지에서 확인해 주세요.</p>'
                    '<img src="https://example.org/poster.png" alt="채용 공고">')], 1))
        self.assertIn("본문", self.detail(10)["review_reason"])
        self.assertIn("이미지", self.detail(11)["review_reason"])
        for status in ("uncertain", "failed", "ready"):
            with sqlite3.connect(self.jobs) as db:
                doc = json.loads(db.execute("SELECT document FROM posts WHERE id=2").fetchone()[0])
                doc["poster_evidence"] = {"status": status, "reason": "표 안의 직무별 요건을 구분할 수 없어요."}
                db.execute("UPDATE posts SET document=? WHERE id=2", (json.dumps(doc),))
            if status == "ready":
                self.assertIn(2, self.items("recommended"))
            else:
                self.assertNotIn(2, self.items("recommended"))
                self.assertEqual(self.detail(2)["review_reason"], doc["poster_evidence"]["reason"])
                self.assertIn(2, self.items("processing_hold"))

    def test_current_failed_attempt_is_reported_without_its_private_message(self):
        from job_search import fit_work
        fit_work.migrate(self.jobs, self.state, profile={})
        with sqlite3.connect(self.jobs) as db:
            db.execute("UPDATE posts SET fit_review=NULL,decision=NULL WHERE id=5")
            db.execute("UPDATE fit_items SET status='failed' WHERE post_id=5")
        self.assertIn("평가 오류", self.detail()["review_reason"])
        self.assertIn(5, self.items("processing_hold"))
