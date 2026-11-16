import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import collect_jobs
import hermes_collect


class HermesDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "job_search"
        self.root.mkdir()
        self.ledger = self.home / ".hermes/cron/executions.db"
        self.ledger.parent.mkdir(parents=True)
        with sqlite3.connect(self.ledger) as db:
            db.execute("CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT,delivery_outcome TEXT)")
            db.execute("INSERT INTO executions VALUES('current','fixture-job','running',NULL)")
        db = collect_jobs.connect(self.root / "data/jobs.sqlite")
        db.close()

    def run_wrapper(self, url=None):
        config = {"job_id": "fixture-job"}
        if url is not None:
            config["dashboard_url"] = url
        (self.root / "deployment.json").write_text(json.dumps(config))
        output = io.StringIO()
        with patch.object(Path, "home", return_value=self.home), patch.object(hermes_collect.subprocess, "run", return_value=SimpleNamespace(stdout='{"new":0}')) as collect, contextlib.redirect_stdout(output):
            hermes_collect.main()
        return json.loads(output.getvalue()), collect.call_count

    def test_dashboard_url_optional(self):
        payload, calls = self.run_wrapper("https://private.example:8443/")
        self.assertEqual(payload["dashboard_url"], "https://private.example:8443")
        self.assertEqual(calls, 1)
        self.assertNotIn("dashboard_url", self.run_wrapper()[0])
        for invalid in ["http://localhost:8765", "https://x.example/jobs", "https://x.example?q=1", "https://x.example#x", "https://u:p@x.example", "https://x.example\n", {"url": "https://x.example"}]:
            with self.subTest(invalid=invalid):
                self.assertNotIn("dashboard_url", self.run_wrapper(invalid)[0])

    def test_retry_digest_unchanged(self):
        original = "상세 조건과 지원 링크\nhttps://private.example/jobs/123"
        with sqlite3.connect(self.root / "data/jobs.sqlite") as db:
            db.execute("INSERT INTO outbox VALUES(?,?,?,0)", ("earlier", original, '["failed-run"]'))
        payload, calls = self.run_wrapper("https://private.example")
        self.assertEqual(calls, 0)
        self.assertEqual(payload["retry_digests"], [original])

    def test_rollback_keeps_saved_digest(self):
        original = "기존 상세 추천 https://private.example/jobs/123"
        with sqlite3.connect(self.root / "data/jobs.sqlite") as db:
            db.execute("INSERT INTO outbox VALUES(?,?,?,0)", ("earlier", original, '["failed-run"]'))
        payload, calls = self.run_wrapper()
        self.assertNotIn("dashboard_url", payload)
        self.assertEqual(payload["retry_digests"], [original])
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
