import io
import json
import sys
import unittest
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import Mock, patch

from PIL import Image

from job_search import poster_model


class PosterModelTests(unittest.TestCase):
    def hermes_runtime(self):
        auxiliary = Mock()
        loader = Mock()
        self.enterContext(patch.dict(sys.modules, {"agent": Mock(auxiliary_client=auxiliary),
                                                   "agent.auxiliary_client": auxiliary,
                                                   "hermes_cli.env_loader": loader}))
        self.enterContext(patch.dict("os.environ", {"HERMES_HOME": "/test/hermes"}))
        self.enterContext(patch("sys.stdin", io.StringIO("[]")))
        self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        return auxiliary

    def test_uses_hermes_output_token_parameter_for_resolved_vision_model(self):
        auxiliary = self.hermes_runtime()
        client = Mock()
        auxiliary.resolve_vision_provider_client.return_value = ("openai", client, "test-model")
        auxiliary.auxiliary_max_tokens_param.return_value = {"max_completion_tokens": 6000}
        reply = Mock(finish_reason="stop")
        reply.message.content = "{}"
        client.chat.completions.create.return_value.choices = [reply]

        poster_model.main()

        auxiliary.auxiliary_max_tokens_param.assert_called_once_with(6000, model="test-model")
        client.chat.completions.create.assert_called_once_with(
            model="test-model", messages=[], max_completion_tokens=6000, timeout=120)
        self.assertEqual(sys.stdout.getvalue(), "{}")

    def test_failure_resolving_credentials_is_retryable_before_submission(self):
        auxiliary = self.hermes_runtime()
        auxiliary.resolve_vision_provider_client.side_effect = RuntimeError("missing credentials")
        with self.assertRaises(SystemExit) as failure:
            poster_model.main()
        self.assertEqual(failure.exception.code, 78)

    def test_legacy_hermes_keeps_its_existing_token_parameter_and_does_not_retry_a_lost_response(self):
        auxiliary = self.hermes_runtime()
        del auxiliary.auxiliary_max_tokens_param
        client = Mock()
        auxiliary.resolve_vision_provider_client.return_value = ("codex", client, "test-model")
        client.chat.completions.create.side_effect = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            poster_model.main()
        client.chat.completions.create.assert_called_once_with(
            model="test-model", messages=[], max_tokens=6000, timeout=120)

    @staticmethod
    def image(size=(400, 2400)):
        output = io.BytesIO()
        Image.new("RGB", size, "white").save(output, "PNG")
        return output.getvalue()

    def test_single_request_preserves_source_row_in_second_tile(self):
        reply = {"complete": True, "lines": [
            {"tile": 2, "text": "개발 | 자격요건 | Python 개발 경험", "bbox": [100, 100, 900, 200]}]}
        with patch("job_search.fit_hermes.run_adapter", return_value=CompletedProcess(
                [], 0, stdout=json.dumps(reply))) as run:
            result = poster_model.read_poster_model(self.image())
        run.assert_called_once()
        self.assertEqual("ready", result["status"])
        self.assertEqual("개발 | 자격요건 | Python 개발 경험", result["lines"][0]["text"])
        x, y, w, h = result["lines"][0]["bbox"]
        self.assertEqual((40, 320), (x, w))
        self.assertTrue(1300 <= y < 2400 and 0 < h < 2400 - y)
        request = json.loads(run.call_args.kwargs["payload"])
        self.assertEqual(2, sum(part["type"] == "image_url" for part in request[1]["content"]))

    def test_incomplete_table_or_unreadable_digits_keep_status_uncertain(self):
        reply = {"complete": False, "lines": [
            {"tile": 1, "text": "모집 부문", "bbox": [10, 10, 250, 20]}]}
        with patch("job_search.fit_hermes.run_adapter", return_value=CompletedProcess(
                [], 0, stdout=json.dumps(reply))):
            result = poster_model.read_poster_model(self.image())
        self.assertEqual("uncertain", result["status"])
        self.assertEqual("모집 부문", result["lines"][0]["text"])

    def test_invalid_rows_cannot_mark_poster_ready(self):
        for bad in ({"tile": 9, "bbox": [0, 0, 1000, 1000]},
                    {"tile": 1, "bbox": [5, 10, 2, 25]},
                    {"tile": 1, "bbox": [0, 0, True, 1000]}):
            with self.subTest(bad=bad):
                reply = {"complete": True, "lines": [{"text": "필수", **bad}]}
                with patch("job_search.fit_hermes.run_adapter", return_value=CompletedProcess(
                        [], 0, stdout=json.dumps(reply))):
                    result = poster_model.read_poster_model(self.image())
                self.assertNotEqual("ready", result["status"])
                self.assertEqual([], result["lines"])

    def test_timeout_or_unavailable_runtime_never_retries(self):
        for error in (FileNotFoundError(), TimeoutExpired("hermes", 120)):
            with self.subTest(error=error):
                with patch("job_search.fit_hermes.run_adapter", side_effect=error) as run:
                    result = poster_model.read_poster_model(self.image())
                self.assertEqual("failed", result["status"])
                run.assert_called_once()

    def test_invalid_or_oversized_poster_never_calls_model(self):
        with patch("job_search.fit_hermes.run_adapter") as run:
            for data in (b"not an image", self.image((300, 20000))):
                with self.subTest(size=len(data)):
                    self.assertNotEqual("ready", poster_model.read_poster_model(data)["status"])
            run.assert_not_called()
