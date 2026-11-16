import tempfile
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
