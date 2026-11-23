import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from job_search import collect_jobs, dashboard_store
from tests.test_collect_jobs import posting


class LogoRequeueTests(unittest.TestCase):
    def test_retry_missing_signals_only_newly_queued_jobs_without_an_existing_logo(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "jobs.sqlite"
            signals = Path(str(db_path) + ".logo-requests")
            collect_jobs.collect(db_path, lambda _: ([posting(i) for i in range(1, 6)], 1))
            state = Path(directory) / "custom-state.sqlite"
            dashboard_store.init_state_db(db_path, state)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("UPDATE posts SET decision='recommend' WHERE id < 5")
                db.execute("INSERT INTO company_logos VALUES ('cached', 'page', 'image', X'00', 1)")
                records = [(1, "missing", None), (2, "pending", None), (3, "ready", "cached"),
                           (4, "missing", "cached"), (5, "missing", None)]
                db.executemany("""INSERT INTO job_company_logos
                    (post_id,outcome,company_key,fingerprint,document_version,identity,attempted_at)
                    VALUES(?,?,?,'v','v','id',1)""", records)
            signals.mkdir()
            command = [sys.executable, "-m", "job_search.company_logos", "--db", str(db_path), "--state-db", str(state), "--retry-missing"]
            for count in (1, 0):
                result = subprocess.run(command, check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(result.stdout), {"requeued": count})
            with sqlite3.connect(db_path) as db:
                rows = db.execute("SELECT outcome,attempted_at FROM job_company_logos ORDER BY post_id").fetchall()
            self.assertEqual(rows, [("pending", None), ("pending", 1), ("ready", 1), ("missing", 1), ("missing", 1)])
            self.assertEqual(len(list(signals.iterdir())), 1)
