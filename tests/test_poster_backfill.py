import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs, poster_backfill

from tests.test_poster_evidence import posting


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.jobs = self.base / "jobs.sqlite"
        db = collect_jobs.connect(self.jobs)
        try:
            for post_id, extra in [
                (1, {"lines": ["지원 마감 2020-01-01"]}),
                (2, {}),
                (3, {"apply_url": "https://example.org/apply", "lines": ["지원 마감 2099-01-01"]}),
                (4, {"poster_evidence": {"version": 1, "status": "uncertain",
                                          "source_lines": [], "reason": "표 매핑을 확인해야 합니다."}}),
            ]:
                with db:
                    collect_jobs.save_document(db, {**posting(), "id": post_id, **extra})
        finally:
            db.close()

    def test_dry_run_is_read_only_prioritizes_open_and_skips_inspected_rows(self):
        before = self.jobs.read_bytes()
        run = subprocess.run([sys.executable, "-m", "job_search.poster_backfill", "--db", str(self.jobs),
                              "--dry-run", "--limit", "2"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual([r["id"] for r in json.loads(run.stdout)["posts"]], [3, 2])
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertEqual(list(self.base.iterdir()), [self.jobs])

    def test_transient_failures_rejoin_open_batch_only_after_cooldown(self):
        db = collect_jobs.connect(self.jobs)
        with db:
            for i, retry_after in [(5, 0), (6, 9999999999)]:
                collect_jobs.save_document(db, {**posting(), "id": i, "poster_evidence": {
                    "version": 1, "status": "failed", "source_lines": [], "images": [
                        {"reason_code": "download_failed", "retry_after": retry_after}]}})
        db.close()
        rows = poster_backfill.run(self.jobs, None, dry_run=True, limit=25)["posts"]
        self.assertEqual([r["id"] for r in rows], [3, 2, 5])

    def test_corrupt_legacy_post_cannot_block_other_candidates(self):
        db = collect_jobs.connect(self.jobs)
        with db:
            db.execute("INSERT INTO posts(id,document,fingerprint) VALUES(9,'broken','old')")
        db.close()
        rows = poster_backfill.run(self.jobs, None, dry_run=True)["posts"]
        self.assertEqual([r["id"] for r in rows], [3, 2])
        with self.assertRaisesRegex(ValueError, "9"):
            poster_backfill.run(self.jobs, None, post_id=9)

    def test_unsuccessful_attempt_does_not_monopolize_the_next_scheduled_slot(self):
        db = collect_jobs.connect(self.jobs)
        try:
            with db:
                db.execute("CREATE TABLE fit_delivery(post_id INTEGER PRIMARY KEY, attempted_at REAL)")
                db.execute("INSERT INTO fit_delivery VALUES(2, 12345)")
                collect_jobs.save_document(db, {**posting(), "id": 8})
            rows = poster_backfill.select_posts(db, None, limit=2, oldest_first=True)
            self.assertEqual([r["id"] for r in rows], [3, 8])
        finally:
            db.close()

    def test_explicit_model_requires_one_post_and_expired_post_skips_work(self):
        run = subprocess.run([sys.executable, "-m", "job_search.poster_backfill", "--db", str(self.jobs), "--model"],
                             capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        with patch("job_search.poster_backfill.prepare_post", return_value="") as prepare:
            poster_backfill.run(self.jobs, None, post_id=4, allow_model=True)
            prepare.assert_called_once_with(self.jobs, None, 4, allow_model=True)
            prepare.reset_mock()
            result = poster_backfill.run(self.jobs, None, post_id=1)
            self.assertEqual(result["posts"][0]["availability"], "closed")
            prepare.assert_not_called()
