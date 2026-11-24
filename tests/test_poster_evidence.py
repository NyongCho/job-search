import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from job_search import poster_evidence as evidence


URL = "https://example.org/recruitment.jpg"


def posting(url=URL):
    return {"id": 42, "title": "기업 모집", "modified_gmt": "2026-03-01", "lines": ["지원하기"],
            "links": [{"text": "지원하기"}], "images": [{"url": url, "alt": ""}],
            "content_status": "needs_review"}


def result(status="ready", reason="ocr_ready"):
    return {"status": status, "reason": reason,
            "lines": [{"text": "고객 문의를 분석하고 서비스 운영을 개선합니다", "bbox": [5, 20, 580, 34]}]}


class PosterEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(self.temp) / "jobs.sqlite"
        self.fetch = self.enterContext(patch.object(evidence, "fetch_public", return_value=(URL, b"poster", "image/jpeg")))
        self.local = self.enterContext(patch.object(evidence, "read_poster", return_value=result()))
        self.model = self.enterContext(patch.object(evidence, "read_poster_model", return_value=result("ready", "model_read")))

    def enrich(self, doc, **kwargs):
        return evidence.enrich_document(self.db, doc, **kwargs)

    def test_traceable_rows_and_cache_across_aliases_revisions_and_workers(self):
        with ThreadPoolExecutor(2) as pool:
            doc, same = list(pool.map(self.enrich, [posting(), posting()]))
        self.assertEqual(doc, same)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.enrich(doc), doc)
        im, = doc["poster_evidence"]["images"]
        self.assertEqual(im["url"], URL)
        self.assertEqual(im["lines"][0]["line_number"], 2)
        self.assertEqual(doc["lines"][1], im["lines"][0]["text"])
        self.assertEqual(im["lines"][0]["bbox"], [5, 20, 580, 34])
        self.enrich(posting(URL + "?v=2"))
        self.enrich({**posting(), "modified_gmt": "2026-04-02"})
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(self.local.call_count, 1)
        self.model.assert_not_called()

    def test_uncertain_rows_are_audit_only_and_model_is_explicit_deduplicated(self):
        self.local.return_value = result("uncertain", "ambiguous_layout")
        held = self.enrich(posting())
        self.assertEqual(held["lines"], ["지원하기"])
        self.assertEqual(held["poster_evidence"]["status"], "uncertain")
        self.assertIn("표", held["poster_evidence"]["reason"])
        self.model.assert_not_called()
        with ThreadPoolExecutor(2) as pool:
            again = list(pool.map(lambda _: self.enrich(held, allow_model=True), range(2)))
        self.assertEqual(again[0]["poster_evidence"]["status"], "ready")
        self.assertEqual(again[0]["poster_evidence"]["images"][0]["method"], "model")
        self.assertEqual(self.enrich(posting()), again[0])
        self.model.assert_called_once()
        self.fetch.assert_called_once()

    def test_failed_or_interrupted_model_never_charges_again_for_same_image(self):
        self.local.return_value = result("uncertain", "low_confidence")
        self.model.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.enrich(posting(), allow_model=True)
        held = self.enrich(posting(), allow_model=True)
        self.assertNotEqual(held["poster_evidence"]["status"], "ready")
        self.assertEqual(held["lines"], ["지원하기"])
        self.model.assert_called_once()

    def test_official_text_decoration_and_unavailable_ocr_do_not_launch_model(self):
        for doc in ({**posting(), "lines": ["주요 업무", "서비스 운영 데이터를 분석합니다", "자격 요건", "Python 활용 경험"]},
                    posting("https://example.org/logo.png")):
            self.assertEqual(self.enrich(doc, allow_model=True), doc)
        self.fetch.assert_not_called()
        self.local.return_value = {"status": "failed", "reason": "tesseract_unavailable", "lines": []}
        held = self.enrich(posting(), allow_model=True)
        self.assertEqual(held["poster_evidence"]["status"], "failed")
        self.assertIn("OCR", held["poster_evidence"]["reason"])
        self.model.assert_not_called()

    def test_verified_official_short_text_is_used_even_with_an_unnamed_photo(self):
        doc = {**posting(), "jd_source": {"url": "https://example.org/apply"},
               "lines": ["필수 자격은 한국어 업무 소통 경험입니다."]}
        self.assertEqual(self.enrich(doc), doc)
        self.fetch.assert_not_called()

    def test_text_jd_in_conversational_korean_does_not_read_a_product_photo(self):
        doc = {**posting(), "lines": [
            "합류하게 되면 이런 일을 하게 됩니다", "재무 관련 서류 관리와 전사 비용전표 처리를 하게 돼요.",
            "해당 부서의 기타 사무 보조 업무도 담당해요.",
            "이런 경험을 가지신 분을 찾고 있어요", "전문 학사 이상의 학력을 가진 분을 원해요.",
            "관련 업무 경험이나 ERP 사용 경험이 있는 분이라면 더욱 좋아요."]}
        self.assertEqual(self.enrich(doc), doc)
        self.fetch.assert_not_called()

    def test_long_pointer_with_two_headings_still_reads_poster(self):
        doc = {**posting(), "lines": ["모집 분야별 담당 업무 및 자격 요건은 아래를 참고하시기 바랍니다."]}
        self.assertEqual(self.enrich(doc)["poster_evidence"]["status"], "ready")

    def test_slow_poster_recovers_once_after_cached_failure_expires(self):
        clock = self.enterContext(patch.object(evidence, "time"))
        clock.time.return_value = 1000
        clock.monotonic.return_value = 200
        budgets = []

        def slow_fetch(url, *, deadline, limit):
            self.assertEqual((url, limit), (URL, 16 * 1024 * 1024))
            budgets.append(deadline - clock.monotonic())
            if budgets[-1] < 29.8:
                raise TimeoutError
            return URL, b"poster", "image/jpeg"

        self.fetch.side_effect = slow_fetch
        failed = self.enrich(posting())
        image, = failed["poster_evidence"]["images"]
        self.assertEqual((image["reason_code"], image["retry_after"]), ("download_failed", 1600))
        clock.time.return_value = 1599
        self.assertEqual(self.enrich(failed, allow_model=True), failed)
        self.assertEqual(budgets, [12])
        self.local.assert_not_called()

        clock.time.return_value = 1600
        with ThreadPoolExecutor(2) as pool:
            ready, concurrent = pool.map(self.enrich, [failed, failed])
        self.assertEqual(ready["poster_evidence"]["status"], "ready")
        self.assertEqual(concurrent, ready)
        self.assertEqual(self.enrich(posting()), ready)
        self.assertEqual(budgets, [12, 40])
        self.local.assert_called_once()
        self.model.assert_not_called()

    def test_failed_slow_retry_restarts_cooldown_and_new_revision_starts_fast(self):
        clock = self.enterContext(patch.object(evidence, "time"))
        clock.time.return_value = 1000
        clock.monotonic.return_value = 0
        self.fetch.side_effect = TimeoutError
        doc = posting()
        urls = [f"{URL}?page={page}" for page in range(1, 6)]
        doc["images"] = [{"url": url} for url in urls]
        self.enrich(doc)
        clock.time.return_value = 1600
        failed = self.enrich(doc)
        self.assertEqual([im["retry_after"] for im in failed["poster_evidence"]["images"]], [2200] * 4)
        clock.time.return_value = 2199
        self.assertEqual(self.enrich(doc, allow_model=True), failed)
        self.enrich({**doc, "modified_gmt": "2026-04-02"})
        attempts = [(call.args[0], call.kwargs["deadline"]) for call in self.fetch.call_args_list]
        self.assertEqual(attempts, [(url, budget) for budget in (12, 40, 12) for url in urls[:4]])
        self.local.assert_not_called()
        self.model.assert_not_called()

    def test_download_failure_is_stable_and_does_not_hide_another_image(self):
        doc = posting()
        doc["images"].append({"url": "https://example.org/second.jpg"})
        self.fetch.side_effect = [OSError("private diagnostic"), (URL, b"poster", "image/jpeg")]
        first = self.enrich(doc)
        self.assertEqual(self.enrich(doc), first)
        self.assertEqual(first["poster_evidence"]["status"], "failed")
        self.assertEqual(len(first["poster_evidence"]["images"]), 2)
        self.assertNotIn("private diagnostic", str(first))
        self.assertEqual(first["lines"], doc["lines"])
        self.assertEqual(self.fetch.call_count, 2)
