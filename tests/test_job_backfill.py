import subprocess
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search import run_job_backfill as backfill


class ExecutionResultTests(unittest.TestCase):
    def test_latest_execution_uses_local_job_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'deployment.json').write_text(json.dumps({'job_id': 'configured-job'}))
            ledger = root / 'executions.db'
            with sqlite3.connect(ledger) as db:
                db.execute('CREATE TABLE executions(id,job_id,status,delivery_outcome,error,claimed_at)')
                db.executemany('INSERT INTO executions VALUES(?,?,?,?,?,?)', [
                    ('chosen', 'configured-job', 'completed', 'delivered', None, '2026-01-01'),
                    ('unrelated', 'other-job', 'running', None, None, '2026-01-02')])
            with patch.object(backfill, 'ROOT', root):
                result = backfill.latest_execution(ledger)
                self.assertIsNotNone(result)
                self.assertEqual(result['id'], 'chosen')

    def test_completion_counts_distinguish_reviewed_jobs_from_applicant_questions(self):
        counts = {"pending": 0, "recommend": 2, "exclude": 1, "needs_review": 5}
        self.assertEqual(backfill.completion_counts(counts), "검토 완료 8건 · 추천 판정 2건 · 제외 판정 1건")

    def test_completion_notice_requires_confirmed_delivery(self):
        for outcome in (None, "failed", "suppressed", "queued"):
            with self.assertRaises(RuntimeError):
                backfill.verify_completion_notice({"status": "completed", "delivery_outcome": outcome})
        backfill.verify_completion_notice({"status": "completed", "delivery_outcome": "delivered"})

    def result(self, code):
        return subprocess.CompletedProcess(["hermes", "cron", "run"], code,
                                           stdout="Ran now: succeeded.", stderr="")

    def test_completed_execution_continues_despite_cli_failure(self):
        record = {"id": "new", "status": "completed", "delivery_outcome": "delivered", "error": None}
        backfill.verify_execution(self.result(1), record, "old")

    def test_failed_execution_is_not_hidden_by_zero_exit(self):
        record = {"id": "new", "status": "failed", "error": "Model failed"}
        with self.assertRaisesRegex(RuntimeError, "Model failed"):
            backfill.verify_execution(self.result(0), record, "old")

    def test_previous_success_does_not_prove_new_run_completed(self):
        record = {"id": "old", "status": "completed", "delivery_outcome": "delivered"}
        with self.assertRaises(RuntimeError):
            backfill.verify_execution(self.result(1), record, "old")

    def test_completed_review_with_failed_delivery_reaches_outbox_retry(self):
        record = {"id": "new", "status": "completed", "delivery_outcome": "failed"}
        backfill.verify_execution(self.result(0), record, "old")


if __name__ == "__main__":
    unittest.main()
