import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from job_search.card_metadata import create_logo_tables


class LogoRequeueTests(unittest.TestCase):
    def test_retry_missing_signals_only_newly_queued_jobs_without_an_existing_logo(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "jobs.sqlite"
            signals = Path(str(db_path) + ".logo-requests")
            signals.mkdir()
            with sqlite3.connect(db_path) as db:
                create_logo_tables(db)
                db.execute("INSERT INTO company_logos VALUES ('cached', 'page', 'image', X'00', 1)")
                records = [(1, "missing", None), (2, "pending", None), (3, "ready", "cached"),
                           (4, "missing", "cached")]
                db.executemany("""INSERT INTO job_company_logos
                    (post_id,outcome,company_key,fingerprint,document_version,identity,attempted_at)
                    VALUES(?,?,?,'v','v','id',1)""", records)
            command = [sys.executable, "-m", "job_search.company_logos", "--db", str(db_path), "--retry-missing"]
            for count in (1, 0):
                result = subprocess.run(command, check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(result.stdout), {"requeued": count})
            with sqlite3.connect(db_path) as db:
                rows = db.execute("SELECT outcome,attempted_at FROM job_company_logos ORDER BY post_id").fetchall()
            self.assertEqual(rows, [("pending", None), ("pending", 1), ("ready", 1), ("missing", 1)])
            self.assertEqual(len(list(signals.iterdir())), 1)
