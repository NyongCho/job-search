import io
import sqlite3
import sys
import tempfile
import json
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs as jobs


def posting(post_id=1, content=None, categories=None):
    return {
        "id": post_id, "title": {"rendered": "회사｜엔지니어"},
        "link": f"https://inthiswork.com/archives/{post_id}",
        "date_gmt": "2026-11-14T00:00:00", "modified_gmt": "2026-11-14T00:00:00",
        "categories": [191700167] if categories is None else categories,
        "content": {"rendered": content or '<h2>우대사항</h2><p>관련 경험 2년</p><a href="https://example.com/job">지원하러 가기</a>'},
    }


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "jobs.sqlite"
        self.now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)

    def collect(self, data, now=None):
        return jobs.collect(self.db, lambda url: (data, 1), now or self.now)

    def review(self, post_id=1, decision="exclude", **extra):
        return {"id": post_id, "fingerprint": jobs.get_posts(self.db, [post_id])[0]["fingerprint"],
                "decision": decision, "reason": "JD grounds", "evidence_lines": [2], **extra}

    def test_collects_full_jd_and_deduplicates_job_categories(self):
        result = self.collect([posting(), posting(2, categories=[191700170]),
                               posting(3, categories=[191700167, 191700169])])
        self.assertEqual(result["new"], 1)
        self.assertEqual(self.collect([posting()])["new"], 0)
        detail = jobs.get_posts(self.db, [1])[0]
        self.assertEqual(detail["lines"][:2], ["우대사항", "관련 경험 2년"])
        self.assertEqual(detail["apply_url"], "https://example.com/job")
        self.assertEqual(len(jobs.pending(self.db)), 1)

    def test_logo_processing_is_deferred_when_collection_has_used_hermes_budget(self):
        post = posting(1, content='<img alt="회사 로고" src="https://cdn.example.com/logo.png">')
        def unexpected_download(url, **_):
            self.fail("Downloads must be deferred when the pre-run budget is nearly spent")
        with patch.object(jobs.time, "monotonic", side_effect=[0, 221, 221]):
            result = jobs.collect(self.db, lambda _: ([post], 1), self.now, logo_fetch=unexpected_download)
        self.assertEqual(result["new"], 1)
        with jobs.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT outcome FROM job_company_logos WHERE post_id=1").fetchone()[0], "pending")
        db.close()

    def test_failed_second_page_does_not_advance_checkpoint_or_save_partial_window(self):
        self.collect([posting()])
        previous = jobs.checkpoint(self.db)
        calls = []

        def failing_page(url):
            calls.append(url)
            if len(calls) == 1:
                return [posting(2)], 2
            raise OSError("temporary network failure")

        with self.assertRaises(OSError):
            jobs.collect(self.db, failing_page, self.now + timedelta(hours=12))
        self.assertEqual(jobs.checkpoint(self.db), previous)
        self.assertEqual([row["id"] for row in jobs.pending(self.db)], [1])

    def test_reads_all_pages_and_overlaps_previous_success(self):
        self.collect([posting()])
        calls = []

        def two_pages(url):
            calls.append(url)
            return ([posting(2 if len(calls) == 1 else 3)], 2)

        result = jobs.collect(self.db, two_pages, self.now + timedelta(hours=12))
        self.assertEqual(result["new"], 2)
        self.assertEqual(len(calls), 2)
        self.assertIn("after=2026-11-13T21%3A00%3A00", calls[0])
        self.assertIn("page=2", calls[1])

    def test_review_is_atomic_and_requires_valid_jd_evidence(self):
        self.collect([posting(), posting(2)])
        valid = self.review(1, "recommend")
        invalid = self.review(2, evidence_lines=[999])
        with self.assertRaises(ValueError):
            jobs.record_reviews(self.db, [valid, invalid])
        self.assertEqual(len(jobs.pending(self.db)), 2)
        jobs.record_reviews(self.db, [valid])
        self.assertEqual([row["id"] for row in jobs.pending(self.db)], [2])
        self.collect([posting(), posting(2)])
        self.assertEqual([row["id"] for row in jobs.pending(self.db)], [2])

    def test_changed_jd_is_queued_again(self):
        self.collect([posting()])
        jobs.record_reviews(self.db, [self.review()])
        updated = posting(content="<p>지원 조건이 변경됨</p>")
        updated["modified_gmt"] = "2026-11-14T01:00:00"
        self.assertEqual(self.collect([updated])["changed"], 1)
        self.assertEqual([row["id"] for row in jobs.pending(self.db)], [1])

    def test_image_only_post_is_retained_as_missing_text(self):
        self.collect([posting(content='<img src="https://example.com/jd.png" alt="채용 안내">')])
        detail = jobs.get_posts(self.db, [1])[0]
        self.assertEqual(detail["lines"], [])
        self.assertEqual(detail["content_status"], "needs_review")
        self.assertEqual(detail["images"][0]["url"], "https://example.com/jd.png")
        jobs.record_reviews(self.db, [self.review(decision="needs_review", evidence_lines=[])])

    def test_missing_or_stale_fingerprint_rejects_entire_batch(self):
        self.collect([posting(), posting(2)])
        old = self.review(2)
        self.collect([posting(2, content="<p>New JD</p><p>Different requirement</p>")])
        missing = self.review(2, evidence_lines=[1])
        del missing["fingerprint"]
        for invalid in (old, missing):
            with self.assertRaises(ValueError):
                jobs.record_reviews(self.db, [self.review(), invalid])
            self.assertEqual(len(jobs.pending(self.db)), 2)

    def test_outbox_survives_failed_and_unrelated_runs_until_delivery_ack(self):
        self.collect([posting()])
        jobs.record_reviews(self.db, [self.review(decision="recommend")], "추천 공고", "run-1")
        retry = jobs.reconcile_outbox(self.db, {"run-1": "failed", "unrelated": "delivered"}, "run-2")
        self.assertEqual(retry, ["추천 공고"])
        self.assertEqual(jobs.reconcile_outbox(self.db, {"run-2": "suppressed"}, "run-3"), ["추천 공고"])
        self.assertEqual(jobs.reconcile_outbox(self.db, {"run-3": "delivered"}, "run-4"), [])
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "run-5"), [])

    def test_invalid_reviews_do_not_save_outbox(self):
        self.collect([posting()])
        with self.assertRaises(ValueError):
            jobs.record_reviews(self.db, [self.review(evidence_lines=[999])], "추천 공고", "run-1")
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "run-2"), [])

    def test_fixed_backfill_window_preserves_checkpoint_and_reviews(self):
        self.collect([posting()])
        jobs.record_reviews(self.db, [self.review()])
        previous = jobs.checkpoint(self.db)
        manifest = Path(self.temp.name) / "backfill.json"
        calls = []
        def page(url):
            calls.append(url)
            return [posting(), posting(2), posting(3, categories=[191700169])], 1
        jobs.prepare_backfill(self.db, manifest, 14, fetch_page=page, now=self.now)
        saved = json.loads(manifest.read_text())
        self.assertEqual(saved["ids"], [1, 2])
        self.assertIn("after=2026-10-31T21%3A00%3A00", calls[0])
        self.assertEqual(jobs.checkpoint(self.db), previous)
        self.assertEqual(jobs.get_posts(self.db, [1])[0]["decision"], "exclude")
        with self.assertRaises(FileExistsError):
            jobs.prepare_backfill(self.db, manifest, 14, fetch_page=page, now=self.now)
        self.assertEqual(len(calls), 1)


class FitReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "jobs.sqlite"

    def collect(self, *ids, content=None):
        posts = [posting(i, content) for i in ids]
        for post in posts:
            post["title"]["rendered"] = f"회사｜직무{post['id']}"
        jobs.collect(self.db, lambda _: (posts, 1))

    def entry(self, post_id, status="met", preference="preferred", **extra):
        doc = jobs.get_posts(self.db, [post_id])[0]
        assessment = {"version": 1, "complete": True, "preference": preference, "eligibility": "eligible",
                      "criteria": [{"key": "core", "label": "장애 대응", "category": "work", "status": status,
                                    "evidence_lines": [2], "evidence": "" if status == "unknown" else "운영 사례 확인"}]}
        if status == "unknown":
            assessment["question"] = "장애 대응을 담당한 경험이 있나요?"
        return {"id": post_id, "fingerprint": doc["fingerprint"], "decision": "recommend",
                "reason": "운영 업무와 직접 관련", "evidence_lines": [2], "availability": "open",
                "apply_url": f"https://example.com/apply/{post_id}", "deadline": "채용 시 마감",
                "required_summary": "서비스 운영 경험", "assessment": assessment, **extra}

    def cli(self, *args):
        capture = io.StringIO()
        with patch.object(sys, "argv", ["collect_jobs", "--db", str(self.db), *args]), redirect_stdout(capture):
            jobs.main()
        return capture.getvalue().rstrip("\n")

    def review_cli(self, entries, *args):
        file = self.db.with_suffix(".json")
        file.write_text(json.dumps(entries, ensure_ascii=False))
        return self.cli("review", str(file), *args)

    def test_fit_migration_preserves_existing_decision_and_outbox(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("""CREATE TABLE posts(id INTEGER PRIMARY KEY, document TEXT NOT NULL,
                          fingerprint TEXT NOT NULL, decision TEXT, reason TEXT, evidence_lines TEXT, reviewed_at TEXT)""")
        self.collect(1)
        entry = self.entry(1)
        del entry["assessment"]
        jobs.record_reviews(self.db, [entry], "기존 추천문", "older")
        with closing(jobs.connect(self.db)) as db:
            self.assertNotIn("fit_review", [r[1] for r in db.execute("PRAGMA table_info(posts)")])
        with self.assertRaisesRegex(ValueError, "migrate-fit"):
            jobs.record_reviews(self.db, [self.entry(1)], execution_id="too-early")
        for _ in range(2):
            self.cli("migrate-fit")
            doc = jobs.get_posts(self.db, [1])[0]
            self.assertEqual(doc["decision"], "recommend")
            self.assertIsNone(doc["fit_review"])
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "retry-old"), ["기존 추천문"])
        jobs.record_reviews(self.db, [self.entry(1)], execution_id="new")
        self.assertEqual(jobs.get_posts(self.db, [1])[0]["fit_review"]["score"], 100)

    def test_equal_scores_use_latest_review_first_like_the_dashboard(self):
        self.collect(1, 2)
        digest = self.review_cli([self.entry(1), self.entry(2)], "--execution-id", "tie")
        self.assertLess(digest.index("직무2"), digest.index("직무1"))

    def test_fit_migration_refuses_missing_and_unrelated_job_databases(self):
        with self.assertRaises((OSError, sqlite3.OperationalError, ValueError)):
            self.cli("migrate-fit")
        self.assertFalse(self.db.exists())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY, body TEXT)")
        with self.assertRaises(ValueError):
            self.cli("migrate-fit")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("posts",)])
            self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(posts)")], ["id", "body"])

    def test_dashboard_origin_is_checked_and_bound_to_saved_delivery(self):
        self.collect(1, 2)
        entries = [self.entry(1), self.entry(2, "unknown")]
        args = ["--execution-id", "with-links", "--dashboard-url"]
        for invalid in ("https://user:pw@dashboard.example", "http://dashboard.example",
                        "https://dashboard.example?x=1", "https://dashboard.example/jobs/300",
                        "https://dashboard.example#x", "https://dashboard.example\n"):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                self.review_cli(entries, *args, invalid)
            self.assertEqual(len(jobs.pending(self.db)), 2)
        body = self.review_cli(entries, *args, "https://dashboard.example:8443/")
        for post_id in (1, 2):
            self.assertIn(f"https://dashboard.example:8443/jobs/{post_id}", body)
        with self.assertRaises(ValueError):
            self.review_cli(entries, *args, "https://another.example")
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "later"), [body])

    def test_only_current_fit_is_read_and_re_review_replaces_it(self):
        self.collect(1)
        entry = self.entry(1)
        jobs.record_reviews(self.db, [entry], execution_id="first")
        with closing(jobs.connect(self.db)) as db, db:
            row = db.execute("SELECT * FROM posts WHERE id=1").fetchone()
            fit = json.loads(row["fit_review"])
            self.assertEqual((fit["score"], fit["reviewed_at"], fit["fingerprint"]),
                             (100, row["reviewed_at"], row["fingerprint"]))
            db.execute("UPDATE posts SET reviewed_at='old collector rewrote the row'")
        self.assertIsNone(jobs.get_posts(self.db, [1])[0]["fit_review"])
        jobs.record_reviews(self.db, [self.entry(1, "partial")], execution_id="second")
        self.assertEqual(jobs.get_posts(self.db, [1])[0]["fit_review"]["score"], 50)
        legacy = {k: entry[k] for k in ("id", "fingerprint", "decision", "reason", "evidence_lines")}
        jobs.record_reviews(self.db, [legacy])
        with closing(jobs.connect(self.db)) as db:
            self.assertIsNone(db.execute("SELECT fit_review FROM posts").fetchone()[0])
        jobs.record_reviews(self.db, [entry], execution_id="third")
        self.collect(1, content="<p>변경 공고</p><p>새 역할</p>")
        with closing(jobs.connect(self.db)) as db:
            self.assertEqual(tuple(db.execute("SELECT decision, fit_review FROM posts").fetchone()), (None, None))
        self.assertIsNone(jobs.get_posts(self.db, [1])[0]["fit_review"])

    def test_scored_cli_groups_orders_and_queues_its_exact_output(self):
        self.collect(1, 2, 3, 4, 5, 6)
        entries = [self.entry(1, "partial"), self.entry(2, preference="outside"), self.entry(3),
                   self.entry(4, "partial", preference="outside"), self.entry(5, "unknown"),
                   self.entry(6, decision="exclude")]
        body = self.review_cli(entries, "--execution-id", "mixed")
        self.assertLess(body.index("직무3"), body.index("직무1"))
        self.assertLess(body.index("직무1"), body.index("직무2"))
        self.assertLess(body.index("직무2"), body.index("직무5"))
        for expected in ("희망 직무", "선호 밖", "확인 질문", "50.0", "100.0", "장애 대응을 담당한 경험이 있나요?",
                         entries[0]["apply_url"], entries[0]["reason"], entries[0]["deadline"],
                         entries[0]["required_summary"], "https://inthiswork.com/archives/1"):
            self.assertIn(expected, body)
        self.assertNotIn("직무4", body)
        self.assertNotIn("직무6", body)
        docs = jobs.get_posts(self.db, [4, 5])
        self.assertEqual([doc["decision"] for doc in docs], ["recommend", "needs_review"])
        self.assertEqual(docs[0]["fit_review"]["score"], 50)
        self.assertEqual(jobs.reconcile_outbox(self.db, {"mixed": "failed"}, "retry"), [body])

    def test_question_only_replay_uses_one_saved_review_and_outbox(self):
        self.collect(1)
        entry = self.entry(1, "unknown")
        args = ["--execution-id", "question-only"]
        body = self.review_cli([entry], *args)
        self.assertIn(entry["assessment"]["question"], body)
        first = jobs.get_posts(self.db, [1])[0]["fit_review"]
        self.assertEqual(self.review_cli([entry], *args), body)
        self.assertEqual(jobs.get_posts(self.db, [1])[0]["fit_review"], first)
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "retry"), [body])
        self.assertEqual(jobs.reconcile_outbox(self.db, {"retry": "delivered"}, "next"), [])
        self.assertEqual(self.review_cli([entry], *args), "[SILENT]")
        self.collect(1, content="<p>변경됨</p><p>검토 필요</p>")
        with self.assertRaises(ValueError):
            self.review_cli([self.entry(1)], *args)
        self.assertEqual(len(jobs.pending(self.db)), 1)

    def test_legacy_cli_keeps_status_output_and_repeated_digest_does_not_enqueue_twice(self):
        self.collect(1)
        entry = self.entry(1)
        del entry["assessment"]
        file = self.db.with_suffix(".txt")
        file.write_text("기존 형식 본문\n")
        for _ in range(2):
            self.assertEqual(json.loads(self.review_cli([entry], "--digest", str(file), "--execution-id", "legacy")),
                             {"recommend": 1})
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "later"), ["기존 형식 본문\n"])

    def test_new_batch_cannot_claim_a_legacy_delivery_or_its_retry_id(self):
        self.collect(1, 2)
        entry = self.entry(1)
        del entry["assessment"]
        jobs.record_reviews(self.db, [entry], "기존 전달 대기", "legacy")
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "retry"), ["기존 전달 대기"])
        for reserved_id in ("legacy", "retry"):
            with self.subTest(id=reserved_id), self.assertRaises(ValueError):
                self.review_cli([self.entry(2, "partial", preference="outside")], "--execution-id", reserved_id)
            self.assertIsNone(jobs.get_posts(self.db, [2])[0]["decision"])

    def test_only_subthreshold_results_are_saved_and_stay_silent_on_retry(self):
        self.collect(1)
        entry = self.entry(1, "partial", preference="outside")
        for _ in range(2):
            self.assertEqual(self.review_cli([entry], "--execution-id", "below"), "[SILENT]")
        self.assertEqual(jobs.reconcile_outbox(self.db, {}, "next"), [])
        self.assertEqual(jobs.get_posts(self.db, [1])[0]["fit_review"]["score"], 50)

    def test_fit_score_never_overrides_eligibility_availability_or_user_uncertainty(self):
        self.collect(1, 2, 3, 4, 5)
        entries = [self.entry(1), self.entry(2, availability="closed"), self.entry(3, availability="unknown"),
                   self.entry(4, decision="needs_review"), self.entry(5, preference="unknown")]
        entries[0]["assessment"]["eligibility"] = "ineligible"
        self.assertEqual(self.review_cli(entries, "--execution-id", "guarded"), "[SILENT]")
        self.assertEqual([j["decision"] for j in jobs.get_posts(self.db, [1, 2, 3, 4, 5])],
                         ["exclude", "exclude", "needs_review", "needs_review", "needs_review"])

    def test_validation_rolls_back_scores_and_outbox_for_bad_delivery_or_assessment(self):
        self.collect(1, 2)
        valid = self.entry(1)
        bad_line = self.entry(2)
        bad_line["assessment"]["criteria"][0]["evidence_lines"] = [999]
        for invalid in (bad_line, self.entry(2, availability=True), self.entry(2, apply_url="javascript:void(0)"),
                        self.entry(2, required_summary=" "), self.entry(2, deadline=None)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.review_cli([valid, invalid], "--execution-id", "bad")
            self.assertEqual(len(jobs.pending(self.db)), 2)
            self.assertEqual(jobs.reconcile_outbox(self.db, {}, "next"), [])



if __name__ == "__main__":
    unittest.main()
