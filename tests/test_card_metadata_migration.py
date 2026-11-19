import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from job_search import collect_jobs, dashboard, dashboard_store, migrate_card_metadata
from scripts.deploy import schema
from tests.test_collect_jobs import posting


class CardMetadataMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.jobs = Path(temporary.name) / "jobs.sqlite"
        self.state = Path(temporary.name) / "state.sqlite"
        self.now = datetime(2026, 11, 19, tzinfo=timezone.utc)
        collect_jobs.collect(self.jobs, lambda _: ([posting()], 1), self.now)
        dashboard_store.init_state_db(self.jobs, self.state)
        self.new_schemas = (schema(self.jobs), schema(self.state))
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("DROP TABLE job_company_logos")
            db.execute("DROP TABLE company_logos")
            db.execute("UPDATE posts SET decision='recommend',reason='old review'")
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("DROP TABLE job_deadline_facts")
            db.execute("DROP TABLE job_deadline_overrides")
            db.execute("INSERT INTO job_state VALUES(1,'applied',NULL,'2026-11-19')")
            db.execute("INSERT INTO application_stages VALUES(1,'interview1','passed','','old memo','2026-11-19')")

    def run_migration(self):
        subprocess.run([sys.executable, "-m", "job_search.migrate_card_metadata",
                        "--jobs-db", str(self.jobs), "--state-db", str(self.state)], check=True, capture_output=True)

    def test_migrating_only_first_db_can_be_resumed_and_legacy_writer_still_works(self):
        before_state = schema(self.state)
        migrate_card_metadata.migrate_database(self.jobs, "jobs")
        self.assertEqual(schema(self.state), before_state)
        self.run_migration()
        self.run_migration()
        self.assertEqual((schema(self.jobs), schema(self.state)), self.new_schemas)
        with closing(sqlite3.connect(self.state)) as db, db:
            # The deployed previous writer inserts into the seven-column table without a column list.
            db.execute("INSERT INTO job_availability VALUES(?,?,?,?,?,?,?)",
                       (1, 1, None, "Legacy closure", "2026-11-20T00:00:00+00:00",
                        "2026-11-20T00:00:00", "https://inthiswork.com/archives/1"))
        job = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(job["reason"], "old review")
        self.assertEqual(job["progress"]["interview1"]["note"], "old memo")
        self.assertEqual(job["status"], "applied")
        self.assertTrue(job["is_expired"])

    def test_unmigrated_health_check_fails_without_creating_new_tables(self):
        schemas = schema(self.jobs), schema(self.state)
        app = dashboard.create_app({
            "TESTING": True, "JOBS_DB": self.jobs, "STATE_DB": self.state, "BASE_URL": "https://dashboard.example",
            "ALLOWED_LOGIN": "owner@example.com", "SECRET_KEY": "test-secret-key-long-enough-123456",
        })
        with app.test_client() as client:
            self.assertEqual(client.get("/healthz", base_url="https://dashboard.example").status_code, 503)
            self.assertEqual((schema(self.jobs), schema(self.state)), schemas)
            self.run_migration()
            self.assertEqual(client.get("/healthz", base_url="https://dashboard.example").status_code, 200)
        self.assertNotEqual(schemas, self.new_schemas)

    def test_invalid_state_is_rejected_before_touching_legacy_jobs_db(self):
        original = schema(self.jobs)
        with self.assertRaises(ValueError):
            migrate_card_metadata.migrate(self.jobs, self.jobs)
        with self.assertRaises(FileNotFoundError):
            migrate_card_metadata.migrate(self.jobs, self.state.with_name("missing.sqlite"))
        self.assertEqual(schema(self.jobs), original)
