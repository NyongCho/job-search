import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs, dashboard_store, fit_work
from job_search.job_availability import document_version
from tests.test_collect_jobs import posting
from tests.test_fit_work import response


class PosterReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.jobs = Path(temp.name) / "jobs.db"
        self.state = self.jobs.with_name("state.db")
        self.url = "https://example.org/poster.jpg"
        self.post = posting(7, f'<img src="{self.url}"><a href="https://example.org/apply">지원하러 가기</a>')
        self.collect()
        self.profile = {"P1": "확인된 근거"}
        dashboard_store.init_state_db(self.jobs, self.state)
        fit_work.migrate(self.jobs, self.state, profile=self.profile)

    def collect(self):
        return collect_jobs.collect(self.jobs, lambda _: ([self.post], 1))

    def read(self):
        with closing(collect_jobs.connect(self.jobs)) as db:
            return json.loads(db.execute("SELECT document FROM posts WHERE id=7").fetchone()[0])

    def enrich(self, path, doc, *, allow_model=False):
        self.assertFalse(allow_model)
        if doc.get("poster_evidence"):
            return doc
        return {**doc, "content_status": "text_available", "lines": [*doc["lines"], "직무 경험과 필수 조건"],
                "poster_evidence": {"status": "ready", "source_lines": doc["lines"],
                                    "source_content_status": doc["content_status"], "reason": "본문 확보"}}

    def test_manual_and_scheduled_assess_the_same_numbered_evidence_once(self):
        before = self.read()
        observed = []

        def evaluate(a, p, docs):
            observed.extend(d["lines"][-1] for d in docs)
            return response(a, p, docs)

        with patch("job_search.poster_review.enrich_document", side_effect=self.enrich) as scan:
            fit_work.refresh_fit_tasks(self.jobs, self.state, [{"id": 20, "post_id": 7, "run_id": 1}],
                                       lambda *_: None, profile=self.profile, runner=evaluate)
            self.assertEqual(document_version(self.read()), document_version(before))
            self.assertEqual(self.collect()["changed"], 0)
            fit_work.scheduled_review(self.jobs, self.state, "next", profile=self.profile,
                                      runner=evaluate, verifier=lambda _: {})
            self.assertEqual(scan.call_count, 2)
        self.assertEqual(observed, ["직무 경험과 필수 조건"])

    def test_lazy_poster_records_source_and_dimensions_without_changing_fit_on_layout_only(self):
        raw = posting(7, '<img src="data:image/gif;base64,AA" data-src="/poster.png" width="700" height="4900">')
        doc = collect_jobs.extract(raw)
        image, = doc["images"]
        self.assertEqual(image, {"url": "https://inthiswork.com/poster.png", "alt": "", "width": 700, "height": 4900})
        old = {**doc, "images": [{"url": image["url"], "alt": ""}]}
        self.assertEqual(fit_work.jd_input(old), fit_work.jd_input(doc))

    def test_old_delivery_is_reassessed_without_another_digest(self):
        from job_search.poster_review import prepare_post
        with sqlite3.connect(self.jobs) as db:
            db.execute("UPDATE fit_delivery SET execution_id='already-delivered' WHERE post_id=7")
        with patch("job_search.poster_review.enrich_document", side_effect=self.enrich):
            self.assertEqual(prepare_post(self.jobs, self.state, 7), "")
            self.assertEqual(fit_work.scheduled_review(self.jobs, self.state, "next", profile=self.profile,
                                                      runner=response), "[SILENT]")
        with sqlite3.connect(self.jobs) as db:
            self.assertEqual(db.execute("SELECT successful FROM fit_attempts").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT execution_id FROM fit_delivery").fetchone(), ("already-delivered",))

    def test_unreadable_evidence_skips_fit_even_if_a_legacy_fit_exists(self):
        def uncertain(path, doc, **kwargs):
            result = self.enrich(path, doc, **kwargs)
            result["lines"] = doc["lines"]
            result["poster_evidence"].update(status="uncertain", reason="직무 표의 행 연결을 확인할 수 없습니다.")
            return result
        # An older evaluator scored a link-only JD before poster reading was available.
        with patch("job_search.poster_review.prepare_post", return_value=""), patch(
                "job_search.poster_review.hold_reason", return_value=""):
            fit_work.refresh_fit_tasks(self.jobs, self.state, [{"id": 1, "post_id": 7}], lambda *_: None,
                                       profile=self.profile, runner=response)
        self.assertIsNotNone(collect_jobs.get_posts(self.jobs, [7])[0]["fit_review"])
        with patch("job_search.poster_review.enrich_document", side_effect=uncertain):
            self.assertEqual(fit_work.scheduled_review(self.jobs, self.state, "hold", profile=self.profile,
                                                      runner=lambda *_: self.fail("판독 보류 건 모델 호출")), "[SILENT]")
        self.assertEqual(self.read()["poster_evidence"]["status"], "uncertain")
        self.assertIsNone(collect_jobs.get_posts(self.jobs, [7])[0]["fit_review"])

    def test_missing_source_does_not_turn_an_application_link_into_a_review_question(self):
        self.post["content"]["rendered"] = '<a href="https://example.org/apply">지원하러 가기</a>'
        self.post["modified_gmt"] = "2026-11-15T00:00:00"
        # A new role with only an apply link cannot inherit another role's poster.
        self.post["title"]["rendered"] = "회사｜기획자"
        self.collect()
        self.assertEqual(fit_work.scheduled_review(self.jobs, self.state, "empty", profile=self.profile,
                                                  runner=lambda *_: self.fail("원문 없는 공고로 평가")), "[SILENT]")

    def test_titles_and_missing_source_notices_do_not_become_model_questions(self):
        from job_search.poster_review import hold_reason
        for lines in (["회사｜엔지니어"], ["엔지니어"], ["첨부파일 참고"], ["공고 원문을 확인해주세요."],
                      ["원문을 찾을 수 없습니다"], ["회사｜엔지니어", "첨부 이미지 참조", "지원하러 가기"]):
            doc = {**self.read(), "images": [], "lines": lines}
            with self.subTest(lines=lines):
                self.assertTrue(hold_reason(doc))

    def test_official_text_recovers_after_a_transient_failure_without_refetching_on_every_run(self):
        from job_search.poster_review import prepare_post
        url = "https://job-boards.greenhouse.io/example/jobs/123"
        self.post["content"]["rendered"] = f'<img src="{self.url}"><a href="{url}">지원하러 가기</a>'
        self.collect()
        official = {"id": 123, "company_name": "회사", "title": "엔지니어", "absolute_url": url,
                    "content": "<p>공식 공고의 필수 자격입니다.</p>"}
        endpoint = "https://boards-api.greenhouse.io/v1/boards/example/jobs/123?content=true"
        with patch("job_search.poster_review.fetch_public", side_effect=[
                TimeoutError(), (endpoint, json.dumps(official).encode(), "application/json")]) as fetch, patch(
                "job_search.poster_review.enrich_document", side_effect=self.enrich), patch("time.time") as clock:
            clock.return_value = 10
            self.assertEqual(prepare_post(self.jobs, self.state, 7), "")
            self.assertIn("poster_evidence", self.read())
            clock.return_value = 11
            self.assertEqual(prepare_post(self.jobs, self.state, 7), "")
            self.assertEqual(fetch.call_count, 1)
            clock.return_value = 1000
            self.assertEqual(prepare_post(self.jobs, self.state, 7), "")
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.read()["lines"], ["공식 공고의 필수 자격입니다."])
        self.assertNotIn("poster_evidence", self.read())

    def test_title_only_official_body_preserves_a_readable_listing_poster(self):
        from job_search.poster_review import prepare_post
        url = "https://job-boards.greenhouse.io/example/jobs/123"
        self.post["content"]["rendered"] = f'<img src="{self.url}"><a href="{url}">지원하러 가기</a>'
        self.collect()
        official = {"id": 123, "company_name": "회사", "title": "엔지니어", "absolute_url": url,
                    "content": "<p>엔지니어</p><p>첨부파일 참고</p>"}
        with patch("job_search.poster_review.fetch_public", side_effect=lambda url, **_: (
                url, json.dumps(official).encode(), "application/json")), patch(
                "job_search.poster_review.enrich_document", side_effect=self.enrich):
            self.assertEqual(prepare_post(self.jobs, self.state, 7), "")
        self.assertEqual(self.read()["poster_evidence"]["status"], "ready")
        self.assertNotIn("jd_source", self.read())

    def test_pre_migration_reviewed_post_keeps_legacy_receipt_while_new_post_delivers_once(self):
        from job_search.poster_review import prepare_post
        older = self.jobs.with_name("older.db")
        source = '<img src="https://example.org/poster.jpg"><p>모집 안내</p><p>지원 마감 2099-12-31</p>' \
                 '<a href="https://example.org/apply">지원하러 가기</a>'
        collect_jobs.collect(older, lambda _: ([posting(7, source), posting(8, source)], 1))
        with sqlite3.connect(older) as db:
            db.execute("UPDATE posts SET decision='needs_review', reason='기존 발송' WHERE id=7")
            db.execute("INSERT INTO outbox VALUES('old','이미 수신한 요약','[]',1)")
        with patch("job_search.poster_review.enrich_document", side_effect=self.enrich):
            for i in (7, 8):
                self.assertEqual(prepare_post(older, self.state, i), "")
            digest = fit_work.scheduled_review(older, self.state, "new", profile=self.profile, runner=response)
            self.assertIn("100.0점", digest)
            self.assertEqual(digest.count("100.0점"), 1)
            self.assertEqual(fit_work.scheduled_review(older, self.state, "repeat", profile=self.profile,
                                                      runner=lambda *_: self.fail("반복 평가")), "[SILENT]")
        with sqlite3.connect(older) as db:
            self.assertEqual(db.execute("SELECT post_id,execution_id FROM fit_delivery ORDER BY 1").fetchall(),
                             [(7, "legacy"), (8, "new")])
            self.assertEqual(db.execute("SELECT successful FROM fit_attempts").fetchone(), (2,))
            self.assertEqual(db.execute("SELECT digest,attempts,delivered FROM outbox WHERE id='old'").fetchone(),
                             ("이미 수신한 요약", "[]", 1))
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone(), (2,))

    def test_revised_listing_cannot_receive_old_poster_lines(self):
        from job_search.poster_review import prepare_post
        def changed(path, doc, **kwargs):
            self.post["modified_gmt"] = "2026-11-15T12:00:00"
            self.post["content"]["rendered"] = "<p>공고가 변경되었습니다.</p>"
            self.collect()
            return self.enrich(path, doc, **kwargs)
        with patch("job_search.poster_review.enrich_document", side_effect=changed):
            self.assertTrue(prepare_post(self.jobs, self.state, 7))
        self.assertEqual(self.read()["lines"], ["공고가 변경되었습니다."])

    def test_removing_a_direct_poster_drops_its_evidence_and_revises_the_fingerprint(self):
        from job_search.poster_review import prepare_post
        with patch("job_search.poster_review.enrich_document", side_effect=self.enrich):
            self.assertEqual(prepare_post(self.jobs, self.state, 7), "")
        before = collect_jobs.get_posts(self.jobs, [7])[0]["fingerprint"]
        self.assertEqual(self.collect()["changed"], 0)
        self.post["modified_gmt"] = "2026-11-16T12:00:00"
        self.post["content"]["rendered"] = '<a href="https://example.org/apply">지원하러 가기</a>'
        self.assertEqual(self.collect()["changed"], 1)
        stored = collect_jobs.get_posts(self.jobs, [7])[0]
        self.assertNotEqual(stored["fingerprint"], before)
        self.assertNotIn("poster_evidence", stored)
        self.assertEqual(stored["images"], [])
        self.assertEqual(stored["lines"], ["지원하러 가기"])

    def test_concurrent_local_and_model_reads_publish_the_ready_result_in_both_orders(self):
        from job_search.poster_review import prepare_post
        for slow_model in (False, True):
            self.post["modified_gmt"] = f"2026-11-{16 + int(slow_model)}T12:00:00"
            self.collect()
            started, release = threading.Event(), threading.Event()

            def delayed(path, doc, *, allow_model=False):
                if allow_model == slow_model:
                    started.set()
                    self.assertTrue(release.wait(3))
                result = self.enrich(path, doc)
                if not allow_model:
                    result["lines"] = doc["lines"]
                    result["poster_evidence"].update(status="uncertain", reason="행 연결 보류")
                return result

            with patch("job_search.poster_review.enrich_document", side_effect=delayed), ThreadPoolExecutor(1) as pool:
                slow = pool.submit(prepare_post, self.jobs, self.state, 7, allow_model=slow_model)
                try:
                    self.assertTrue(started.wait(3))
                    prepare_post(self.jobs, self.state, 7, allow_model=not slow_model)
                finally:
                    release.set()
                self.assertEqual(slow.result(3), "")
                self.assertEqual(self.read()["poster_evidence"]["status"], "ready")

    def test_verified_official_text_comes_before_the_listing_poster(self):
        from job_search.poster_review import prepare_post
        url = "https://job-boards.greenhouse.io/example/jobs/123"
        self.post["content"]["rendered"] = f'<img src="{self.url}"><a href="{url}">지원하러 가기</a>'
        self.collect()
        official = {"id": 123, "company_name": "회사", "title": "엔지니어", "absolute_url": url,
                    "content": "<p>필수 자격은 한국어 업무 소통입니다.</p>"}
        with patch("job_search.poster_review.fetch_public", side_effect=lambda url, **_: (
                url, json.dumps(official).encode(), "application/json")), patch(
                "job_search.poster_review.enrich_document", side_effect=AssertionError("불필요한 판독")):
            self.assertEqual(prepare_post(self.jobs, self.state, 7), "")
        self.assertEqual(self.read()["lines"], ["필수 자격은 한국어 업무 소통입니다."])
        self.assertEqual(self.read()["jd_source"]["original_id"], "123")
        self.assertEqual(self.collect()["changed"], 0)
