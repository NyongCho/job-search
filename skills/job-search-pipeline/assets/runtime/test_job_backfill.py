import subprocess
import unittest

import run_job_backfill as backfill


class ExecutionResultTests(unittest.TestCase):
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
