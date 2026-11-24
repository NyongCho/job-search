import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from functools import partial
from html import unescape
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs
from job_search import dashboard, fit_work, refresh_queue, refresh_sources
from tests.test_collect_jobs import posting
from tests.test_dashboard_store import collect_fit_examples
from tests.test_fit_work import response as scored_response


class WebTests(unittest.TestCase):
    base = "https://dashboard.example"
    identity = {"Tailscale-User-Login": "owner@example.com"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "jobs.sqlite"
        self.state = Path(self.temp.name) / "private/state.sqlite"
        collect_jobs.collect(self.source, lambda _: ([posting()], 1), datetime(2026, 11, 16, tzinfo=timezone.utc))
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE posts SET decision='recommend',reason='Python Agent 업무',evidence_lines='[1]',reviewed_at='2026-11-16T00:00:00Z'")
        self.config = {"TESTING": True, "JOBS_DB": self.source, "STATE_DB": self.state,
                       "BASE_URL": self.base, "ALLOWED_LOGIN": "owner@example.com",
                       "SECRET_KEY": "test-secret-key-long-enough-123456", "DEV_MODE": False}
        self.app = dashboard.create_app(self.config)
        self.client = self.app.test_client()

    def get(self, path="/", client=None, headers=None):
        return (client or self.client).get(path, base_url=self.base, headers=self.identity if headers is None else headers)

    def csrf(self, client=None):
        client = client or self.client
        self.get(client=client)
        with client.session_transaction(base_url=self.base) as session:
            return session["csrf_token"]

    def post(self, path, data, token=None, client=None):
        return (client or self.client).post(path, base_url=self.base, json=data,
                                          headers={**self.identity, "X-CSRF-Token": token or ""})

    def test_identity_required(self):
        for headers in [{}, {"Tailscale-User-Login": "other@example.com"}]:
            self.assertEqual(self.get(headers=headers).status_code, 403)
            self.assertEqual(self.get("/jobs/1", headers=headers).status_code, 403)
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.get("/healthz", headers={}).status_code, 200)
        response = self.client.get("/", base_url="https://evil.example", headers=self.identity)
        self.assertEqual(response.status_code, 400)

    def test_loopback_health_check_requires_the_configured_host(self):
        url = "http://127.0.0.1:8765"
        self.assertEqual(self.client.get("/healthz", base_url=url).status_code, 400)
        health = self.client.get("/healthz", base_url=url, headers={"Host": "dashboard.example"})
        self.assertEqual((health.status_code, health.json), (200, {"status": "ok"}))

    def test_navigation_returns_card_placeholders_before_the_list_query(self):
        path = "/?view=interested&q=없는회사&sort=fit"
        navigation = {**self.identity, "Sec-Fetch-Mode": "navigate"}
        with patch.object(dashboard.store, "list_jobs", side_effect=AssertionError("queried before paint")):
            html = self.get(path, headers=navigation).get_data(as_text=True)
        self.assertIn('aria-busy="true"', html)
        self.assertIn('class="skeleton-card"', html)
        self.assertIn('name="q" value="없는회사"', html)
        self.assertNotIn("검색 결과가 없어요", html)
        data = self.get(path, headers={**self.identity, "X-Dashboard-Snapshot": "1"}).get_data(as_text=True)
        self.assertIn("검색 결과가 없어요", data)
        self.assertNotIn('class="skeleton-card"', data)
        self.assertIn('data-runs=', data)
        fallback = self.get(path + "&render=full", headers=navigation).get_data(as_text=True)
        self.assertIn("검색 결과가 없어요", fallback)

    def test_direct_navigation_displays_cached_detail_before_list_is_available(self):
        with patch.object(dashboard.store, "list_jobs", side_effect=AssertionError("queried before paint")):
            html = self.get("/jobs/1", headers={**self.identity, "Sec-Fetch-Mode": "navigate"}).get_data(as_text=True)
        self.assertIn('id="job-detail"', html)
        self.assertIn("Python Agent 업무", html)
        self.assertIn('aria-label="현재 공고 새로고침"', html)
        self.assertIn('class="skeleton-card"', html)

    def test_split_pane_follows_refresh_for_another_visible_row(self):
        collect_jobs.collect(self.source, lambda _: ([posting(2)], 1), datetime(2026, 11, 16, tzinfo=timezone.utc))
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE posts SET decision='recommend' WHERE id=2")
        run = self.post("/refreshes", {"post_id": 2}, self.csrf())
        self.assertEqual(self.get("/refreshes?post_id=1").json["runs"], [])
        html = self.get("/jobs/1", headers={**self.identity, "X-Dashboard-Snapshot": "1"}).get_data(as_text=True)
        baselines = json.loads(unescape(re.search(r"data-runs='([^']+)'", html)[1]))
        self.assertEqual([item["id"] for item in baselines], [run.json["id"]])

    def test_expired_tab_observes_a_pending_recommendation_entering_the_list(self):
        from job_search.refresh_job_availability import write_facts

        self.post("/refreshes", {"post_id": 1}, self.csrf())
        task, = refresh_queue.claim(self.state, "deadline", 1)
        headers = {**self.identity, "X-Dashboard-Snapshot": "1"}
        before = self.get("/?view=expired", headers=headers).get_data(as_text=True)
        self.assertIn("아직 공고가 없어요", before)
        self.assertIn("data-runs='[]'", before)
        self.assertEqual(json.loads(unescape(re.search(r"data-activity='([^']+)'", before)[1])),
                         {"active": True, "cursor": 0})

        with sqlite3.connect(self.source) as db:
            doc = json.loads(db.execute("SELECT document FROM posts WHERE id=1").fetchone()[0])
        with sqlite3.connect(self.state) as db:
            write_facts(db, 1, doc, dict(closed=True, deadline_at=None, deadline_kind="unknown",
                        deadline_word="", evidence="접수 종료"), datetime.now(timezone.utc),
                        doc["modified_gmt"], doc["url"])
            refresh_queue.finish(self.state, task, "done", db=db)
        activity = self.get("/refreshes/activity").get_json()
        self.assertGreater(activity["cursor"], 0)
        updated = self.get("/?view=expired", headers=headers).get_data(as_text=True)
        self.assertIn('data-job-id="1"', updated)
        self.assertRegex(updated, r'만료 목록 <span>1건</span>')

    def test_sidebar_shows_authenticated_username(self):
        for path in ["/", "/jobs/1", "/jobs/999"]:
            with self.subTest(path=path):
                sidebar = self.get(path).get_data(as_text=True).split("</aside>", 1)[0]
                self.assertIn(">owner<", sidebar)
                self.assertIn('role="img" aria-label="설정"', sidebar)
                self.assertNotIn(">설정<", sidebar)
                self.assertNotIn("owner@example.com", sidebar)
                self.assertNotIn("PRIVATE · TAILSCALE", sidebar)
        denied = self.get(headers={}).get_data(as_text=True)
        self.assertNotIn(">owner<", denied)

    def test_local_preview_profile(self):
        app = dashboard.create_app({**self.config, "DEV_MODE": True, "BASE_URL": "http://localhost"})
        sidebar = app.test_client().get("/").get_data(as_text=True).split("</aside>", 1)[0]
        self.assertIn(">로컬 사용자<", sidebar)

    def test_csrf_write_only(self):
        token = self.csrf()
        other = self.app.test_client()
        other_token = self.csrf(other)
        self.assertNotEqual(token, other_token)
        for bad in [None, other_token]:
            self.assertEqual(self.post("/jobs/1/state", {"status": "applied"}, bad).status_code, 403)
        self.get("/jobs/1")
        with sqlite3.connect(self.state) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_state").fetchone()[0], 0)
        result = self.post("/jobs/1/state", {"status": "interested"}, token)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json, {"status": "interested"})
        self.assertEqual(self.get("/jobs/1/state").status_code, 405)
        for bad in [{"status": []}, {"status": "hired"}, [], None]:
            self.assertEqual(self.post("/jobs/1/state", bad, token).status_code, 400)

    def test_deadline_update_requires_csrf_and_valid_type_specific_fields(self):
        path = "/jobs/1/deadline"
        future = {"mode": "manual", "kind": "datetime", "date": "2099-12-02", "time": "23:59"}
        token = self.csrf()
        self.assertEqual(self.post(path, future).status_code, 403)
        for invalid in [{**future, "date": "2099-02-29"}, {**future, "time": "24:00"},
                        {**future, "kind": "date"}, {"mode": "manual", "kind": []}]:
            self.assertEqual(self.post(path, invalid, token).status_code, 400)
        saved = self.post(path + "?view=recommended&q=Agent&page=1", future, token)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json["deadline_label"], "12.02 23:59 마감")
        body = saved.json["html"]
        self.assertIn('aria-label="마감 정보 수정 · 12.02 23:59 마감"', body)
        self.assertIn('aria-label="현재 공고 새로고침"', body)
        self.assertIn('<dialog id="deadline-dialog"', body)
        self.assertIn('name="q" value="Agent"', body)

    def test_sort_selection_and_navigation(self):
        collect_jobs.collect(self.source, lambda _: ([posting(i) for i in range(2, 32)], 1),
                             datetime(2026, 11, 16, tzinfo=timezone.utc))
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE posts SET decision='recommend', reason='Agent'")
        listing = self.get("/?q=Agent&sort=company").get_data(as_text=True)
        self.assertIn('<option value="company" selected>', listing)
        self.assertNotIn('<option value="activity"', listing)
        links = re.findall(r'href="([^"]+)"', listing)
        for url in (next(u for u in links if "/jobs/" in u), next(u for u in links if "page=2" in u)):
            self.assertIn("sort=company", url)
            self.assertIn("q=Agent", url)
        detail = self.get("/jobs/1?view=recommended&q=Agent&sort=company").get_data(as_text=True)
        self.assertIn("sort=company", re.search(r'class="back-link" href="([^"]+)"', detail)[1])
        for view, value, label in (("applied", "activity", "최근 기록순"), ("recommended", "fit", "적합도순"),
                                   ("recommended&sort=reviewed", "reviewed", "최근 검토순"),
                                   ("expired&sort=deadline", "deadline", "최근 마감순")):
            page = self.get("/?view=" + view).get_data(as_text=True)
            self.assertIn(f'<option value="{value}" selected>{label}</option>', page)

    def test_inbox_views_offer_fit_sort_and_keep_their_defaults(self):
        for view in ("interested", "preparing", "applied", "archived", "needs_review", "processing_hold", "expired"):
            with self.subTest(view=view):
                path = "/?view=" + view
                default = "activity" if view == "applied" else "reviewed"
                menu = self.get(path).get_data(as_text=True)
                self.assertIn(f'<option value="{default}" selected>', menu)
                self.assertIn('<option value="fit">적합도순</option>', menu)
                selected = self.get(path + "&sort=fit")
                self.assertEqual(selected.status_code, 200)
                self.assertIn('<option value="fit" selected>적합도순</option>', selected.get_data(as_text=True))

    def test_interested_fit_sort_keeps_search_and_page_on_detail_return(self):
        collect_fit_examples(self.source)
        for post_id in (1, 2, 3, 4):
            dashboard.store.set_status(self.source, self.state, post_id, "interested")

        def follow(html, link):
            return self.get(unescape(re.search(link, html)[1])).get_data(as_text=True)

        def check_page(html, ids):
            self.assertIn('name="q" value="Agent"', html)
            self.assertIn('<option value="fit" selected>', html)
            self.assertNotIn('class="list-group-title"', html)
            self.assertEqual(re.findall(r'class="job-row[^"]*" href="/jobs/(\d+)\?', html), ids)

        with patch.object(dashboard.store, "PAGE_SIZE", 2):
            first = self.get("/?view=interested&q=Agent&sort=fit").get_data(as_text=True)
            check_page(first, ["3", "4"])
            self.assertIn("90.0점 · 선호 밖 추천", first)
            second = follow(first, r'href="([^"]+)" aria-label="다음 페이지"')
            check_page(second, ["2", "1"])
            self.assertIn("적합도 미산정", second)
            detail = follow(second, r'href="(/jobs/2\?[^"]+)"')
            returned = follow(detail, r'class="back-link" href="([^"]+)"')
            check_page(returned, ["2", "1"])
            previous = follow(returned, r'href="([^"]+)" aria-label="이전 페이지"')
            check_page(previous, ["3", "4"])

    def test_fit_list_and_detail_keep_evidence_questions_and_return_location(self):
        collect_fit_examples(self.source)
        page = self.get("/").get_data(as_text=True)
        headings = re.findall(r'<h2 class="list-group-title">([^<]+)</h2>', page)
        self.assertEqual(headings, ["확인 질문"])
        self.assertIn("75.0점 · 희망 직무 추천", page)
        self.assertIn("90.0점 · 선호 밖 추천", page)
        self.assertIn("적합도 미산정", page)
        self.assertNotIn("79.9점", page)
        self.assertIn("운영 장애에 직접 대응했나요?", page)
        with patch.object(dashboard.store, "PAGE_SIZE", 2):
            page = self.get("/?q=Agent&page=2").get_data(as_text=True)
            location = unescape(re.search(r'href="(/jobs/3\?[^"]+)"', page)[1])
            detail = self.get(location).get_data(as_text=True)
            article = detail.split('<article id="job-detail"', 1)[1]
            self.assertIn("90.0점", article)
            self.assertIn("필수 요건", article)
            self.assertIn("운영 경력 10개월", article)
            self.assertIn("서비스 배포와 모니터링을 9개월 담당", article)
            self.assertIn("L1", article)
            returned = self.get(unescape(re.search(r'class="back-link" href="([^"]+)"', article)[1]))
            self.assertEqual(re.findall(r'class="job-row[^"]*" href="/jobs/(\d+)\?', returned.get_data(as_text=True)),
                             ["3", "1"])
        question = self.get("/jobs/5?view=needs_review").get_data(as_text=True).split('<article id="job-detail"', 1)[1]
        self.assertIn("운영 장애에 직접 대응했나요?", question)
        self.assertIn("공고 적합도 보류", question)
        old = self.get("/jobs/1").get_data(as_text=True).split('<article id="job-detail"', 1)[1]
        self.assertIn("미산정", old)
        self.assertNotIn("0.0점", old)

    def answer_form(self):
        collect_fit_examples(self.source)
        fit_work.migrate(self.source, self.state, profile={"P1": "합성 경력"})
        location = "/jobs/5?view=needs_review&q=Agent&sort=fit"
        html = self.get(location).get_data(as_text=True)
        form = re.search(r'<form[^>]+class="review-answer-form".*?</form>', html, re.S)
        self.assertIsNotNone(form, "An applicant question needs a server-rendered answer form")
        self.assertIn('enctype="multipart/form-data"', form[0])
        action = unescape(re.search(r'action="([^"]+)"', form[0])[1])
        fields = {name: unescape(value) for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form[0])}
        return action, {**fields, "answer": "테스트용 개인 답변입니다."}

    def test_answer_form_is_private_idempotent_and_redirects_to_its_context(self):
        action, fields = self.answer_form()
        self.assertEqual(fields["question"], "운영 장애에 직접 대응했나요?")
        with sqlite3.connect(self.source) as db:
            self.assertEqual(fields["fingerprint"], db.execute("SELECT fingerprint FROM posts WHERE id=5").fetchone()[0])
        for _ in range(2):
            submitted = self.client.post(action, base_url=self.base, headers=self.identity, data=fields)
            self.assertEqual(submitted.status_code, 303)
            self.assertNotIn(fields["answer"], submitted.location + submitted.get_data(as_text=True))
            self.assertIn("/jobs/5?", submitted.location)
            self.assertIn("view=needs_review", submitted.location)
            self.assertIn("sort=fit", submitted.location)
        with sqlite3.connect(self.state) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM refresh_runs").fetchone()[0], 1)
        detail = self.get(submitted.location).get_data(as_text=True)
        self.assertIn("답변을 저장했어요", detail)
        self.assertNotIn('class="review-answer-form"', detail)
        self.assertNotIn(fields["answer"], detail)

    def test_answer_form_rejects_unauthorized_invalid_and_stale_submissions(self):
        action, fields = self.answer_form()
        for changes, headers, expected in [
            ({}, {}, 403), ({"csrf_token": ""}, self.identity, 403),
            ({"answer": " "}, self.identity, 400), ({"fingerprint": "old"}, self.identity, 409),
            ({"question": "변경된 질문"}, self.identity, 409),
        ]:
            with self.subTest(changes=changes, expected=expected):
                result = self.client.post(action, base_url=self.base, headers=headers, data={**fields, **changes})
                self.assertEqual(result.status_code, expected)
                self.assertEqual(result.mimetype, "text/html")
                self.assertNotIn(fields["answer"], result.get_data(as_text=True))
                if expected == 409:
                    self.assertIn("/jobs/5?", result.get_data(as_text=True))

    def test_processing_hold_has_its_reason_and_no_applicant_prompt(self):
        collect_fit_examples(self.source)
        question = "접수 중인지 채용 담당자에게 물어봐 주시겠어요?"
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE posts SET fit_review=json_set(fit_review,'$.question',?) WHERE id=5", (question,))
        for path in ("/?view=processing_hold", "/jobs/5?view=processing_hold"):
            response = self.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("처리 보류", html)
            self.assertNotIn(question, html)
            self.assertNotIn('class="review-answer-form"', html)
            self.assertIn('data-job-id="5"', html)
        self.assertNotIn('data-job-id="5"', self.get("/?view=needs_review").get_data(as_text=True))

    def test_saved_subthreshold_job_shows_score_and_delivery_threshold(self):
        collect_fit_examples(self.source)
        dashboard.store.set_status(self.source, self.state, 4, "interested")
        html = self.get("/jobs/4?view=interested").get_data(as_text=True)
        for section in html.split('<article id="job-detail"', 1):
            self.assertIn("79.9점", section)
            self.assertIn("선호 밖 추천 기준 미만", section)
            self.assertNotIn("추천 제외", section)
        self.assertNotIn("추천 이유</div>", html)

    def test_closing_last_question_updates_recommendation_page_and_count(self):
        collect_fit_examples(self.source)
        token = self.csrf()
        with patch.object(dashboard.store, "PAGE_SIZE", 2):
            path = "/jobs/5/deadline?view=recommended&q=Agent&sort=fit&page=3"
            saved = self.post(path, {"mode": "manual", "kind": "date", "date": "2000-01-01"}, token)
            self.assertEqual(saved.status_code, 200)
            html = saved.json["html"]
            self.assertIn("2 / 2 페이지", html)
            self.assertIn("추천 목록 <span>4건</span>", html)
            self.assertIn('data-scroll-key="recommended:fit:Agent:2"', html)

    def test_invalid_sort_precedes_deadline_write_and_refresh(self):
        token = self.csrf()
        for sort in ("bad", "activity", ""):
            query = "?view=recommended&sort=" + sort
            self.assertEqual(self.get("/" + query).status_code, 400)
            result = self.post("/jobs/1/deadline" + query, {"mode": "manual", "kind": "unknown"}, token)
            self.assertEqual(result.status_code, 400)
            self.assertIsNone(dashboard.store.get_job(self.source, self.state, 1)["deadline_manual"])
            self.assertEqual(self.post("/jobs/1/deadline/refresh" + query, {}, token).status_code, 400)
        with sqlite3.connect(self.state) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM refresh_runs").fetchone()[0], 0)

    def test_deadline_responses_keep_sorted_rows(self):
        collect_jobs.collect(self.source, lambda _: ([posting(2, content="<p>지원 마감: 2099-12-02</p>")], 1),
                             datetime(2026, 11, 16, tzinfo=timezone.utc))
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE posts SET decision='recommend', reason='Agent'")
        token = self.csrf()
        query = "?q=Agent&sort=deadline"
        def row_ids(html):
            return re.findall(r'class="job-row[^"]*" href="/jobs/(\d+)\?', html)
        self.assertEqual(row_ids(self.get("/" + query).get_data(as_text=True)), ["2", "1"])
        saved = self.post("/jobs/1/deadline" + query, {"mode": "manual", "kind": "date", "date": "2099-12-01"}, token)
        refreshed = self.post("/jobs/1/deadline/refresh" + query, {}, token)
        self.assertEqual(refreshed.status_code, 202)
        self.assertEqual(refreshed.json["id"], self.post("/refreshes" + query, {"post_id": 1}, token).json["id"])
        snapshot = self.get("/jobs/1" + query, headers={**self.identity, "X-Dashboard-Snapshot": "1"})
        self.assertEqual(saved.status_code, 200)
        for html in (saved.json["html"], snapshot.get_data(as_text=True)):
            self.assertEqual(row_ids(html), ["1", "2"])
            self.assertIn('<option value="deadline" selected>', html)

    def test_logo_refresh_tracks_dashboard_membership_across_overlapping_clicks(self):
        token = self.csrf()
        dashboard.store.set_status(self.source, self.state, 1, "archived")
        bulk = self.post("/refreshes?view=archived", {}, token)
        self.assertEqual(bulk.status_code, 202)
        self.assertEqual(bulk.json["by_kind"]["logo"]["skipped"], 1)
        self.assertEqual(bulk.json["counts"]["queued"], 2)
        self.assertEqual(refresh_queue.claim(self.state, "logo", 1), [])
        detail = self.post("/refreshes?view=recommended", {"post_id": 1}, token)
        self.assertEqual(detail.json["post_tasks"]["logo"], {
            "status": "skipped", "message": refresh_queue.LOGO_OUT_OF_SCOPE})
        dashboard.store.set_status(self.source, self.state, 1, "interested")
        resumed = self.post("/refreshes", {"post_id": 1}, token)
        self.assertEqual(resumed.json["id"], detail.json["id"])
        self.assertEqual(resumed.json["post_tasks"]["logo"]["status"], "queued")

    def test_imported_application_offers_common_refresh_with_no_deadline_source(self):
        dashboard.store.import_profile_jobs(self.source, self.state, [{
            "source_key": "applications/legacy", "company": "회사", "role": "직무", "summary": "기존 지원",
            "confirmed_on": "2026-11-15", "source_url": "https://inthiswork.com/archives/1",
        }])
        token = self.csrf()
        self.assertEqual(self.post("/jobs/-1/deadline", {"mode": "manual", "kind": "unknown"}, token).status_code, 200)
        self.assertIsNotNone(dashboard.store.get_job(self.source, self.state, -1)["deadline_manual"])
        run = self.post("/jobs/-1/deadline/refresh", {}, token)
        self.assertEqual(run.status_code, 202)
        finish = partial(refresh_queue.finish, self.state)
        def external_call(*args, **kwargs):
            self.fail("a profile record without JD must not reach a source or model")
        refresh_sources.refresh_deadlines(self.source, self.state, refresh_queue.claim(self.state, "deadline", 1),
                                          finish, fetch=external_call)
        logo, = refresh_queue.claim(self.state, "logo", 1)
        refresh_sources.refresh_logo_task(self.source, self.state, logo, finish, fetch=external_call)
        fit_work.refresh_fit_tasks(self.source, self.state, refresh_queue.claim(self.state, "fit", 1),
                                   finish, profile={}, runner=external_call)
        self.assertEqual(self.get(f"/refreshes/{run.json['id']}").json["counts"]["skipped"], 2)
        html = self.get("/jobs/-1").get_data(as_text=True)
        self.assertIn('aria-label="현재 공고 새로고침"', html)
        self.assertIn("공고 적합도 보류", html)
        self.assertIn("원문", html.split('id="fit-heading"', 1)[1].split("</section>", 1)[0])

    def test_historical_fit_evidence_quotes_the_verified_jd(self):
        record = {"title": "회사｜직무", "lines": ["개인 지원 메모"], "jd": {
            "title": "회사｜직무", "url": "https://example.org/jobs/10", "lines": ["필수 운영 경험"],
            "jd_source": {"source": "official", "original_id": "10"},
        }}
        with sqlite3.connect(self.state) as db:
            db.execute("INSERT INTO profile_posts VALUES(-1,'applications/legacy',?,'2026-11-15','2026-11-15')",
                       (json.dumps(record),))
        fit_work.refresh_fit_tasks(self.source, self.state, [{"id": 77, "post_id": -1, "version": ""}],
                                   lambda *args: None, profile={"P1": "운영 경험"}, runner=scored_response)
        html = self.get("/jobs/-1").get_data(as_text=True)
        evidence = html.split('id="fit-heading"', 1)[1].split("</section>", 1)[0]
        self.assertIn("필수 운영 경험", evidence)
        self.assertNotIn("개인 지원 메모", evidence)

    def test_failed_source_request_leaves_existing_manual_value_and_memo(self):
        token = self.csrf()
        self.post("/jobs/1/state", {"status": "applied"}, token)
        self.post("/jobs/1/progress", {"stage": "interview1", "result": "in_progress", "note": "keep memo"}, token)
        self.post("/jobs/1/deadline", {"mode": "manual", "kind": "rolling", "word": "상시 채용"}, token)
        response = self.post("/jobs/1/deadline/refresh", {}, token)
        self.assertEqual(response.status_code, 202)
        def unavailable(*args, **kwargs):
            raise TimeoutError
        refresh_sources.refresh_deadlines(self.source, self.state, refresh_queue.claim(self.state, "deadline", 1),
                                          partial(refresh_queue.finish, self.state), fetch=unavailable)
        changes = self.get(f"/refreshes/{response.json['id']}").json["changes"]
        self.assertEqual([(event["kind"], event["status"]) for event in changes], [("deadline", "failed")])
        job = dashboard.store.get_job(self.source, self.state, 1)
        self.assertEqual(job["deadline_label"], "상시 채용")
        self.assertEqual(job["progress"]["interview1"]["note"], "keep memo")

    def test_unknown_job_and_db_failure(self):
        self.assertEqual(self.get("/jobs/999").status_code, 404)
        token = self.csrf()
        self.assertEqual(self.post("/jobs/999/state", {"status": "applied"}, token).status_code, 404)
        self.source.unlink()
        self.assertEqual(self.get().status_code, 503)
        self.assertEqual(self.get("/healthz", headers={}).status_code, 503)
        self.assertFalse(self.source.exists())

    def test_production_factory_rejects_dev_mode(self):
        settings = {"JOBS_DB": str(self.source), "DASHBOARD_STATE_DB": str(self.state),
                    "DASHBOARD_BASE_URL": "http://localhost:8765", "DASHBOARD_DEV_MODE": "1",
                    "DASHBOARD_ALLOWED_LOGIN": "owner@example.com", "DASHBOARD_SECRET_KEY": self.config["SECRET_KEY"]}
        with patch.dict(os.environ, settings), self.assertRaises(ValueError):
            dashboard.create_production_app()
        with self.assertRaises(ValueError):
            dashboard.create_app({**self.config, "DEV_MODE": True})

    def test_external_content_and_security_headers(self):
        with sqlite3.connect(self.source) as db:
            document = json.loads(db.execute("SELECT document FROM posts").fetchone()[0])
            document.update(title='<script>alert(1)</script>｜" onfocus="alert(2)',
                            apply_url="javascript:alert(3)", lines=['<img src=x onerror="alert(4)">'])
            db.execute("UPDATE posts SET document=?,reason=?", (json.dumps(document), '<script>alert(5)</script>'))
        result = self.get("/jobs/1")
        body = result.get_data(as_text=True)
        self.assertNotIn('<script>alert', body)
        self.assertNotIn('href="javascript:', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", result.headers["Content-Security-Policy"])
        self.assertIn("HttpOnly", result.headers.get("Set-Cookie", ""))
        self.assertIn("Secure", result.headers.get("Set-Cookie", ""))

    def test_display_dates_and_unknown_recruitment(self):
        result = self.get("/jobs/1")
        text = result.get_data(as_text=True)
        self.assertIn("2026.11.16 09:00", text)
        self.assertIn("마감일 미확인", text)
        self.assertIn("마지막 수집", text)
        self.assertIn("추천 검토", text)
        self.assertNotIn("D-day", text)
        self.assertIn('id="job-detail"', text)
        self.assertIn("csrf-token", text)

    def test_expired_recommendation_is_separate_and_keeps_original_link(self):
        with sqlite3.connect(self.source) as db:
            doc = json.loads(db.execute('SELECT document FROM posts').fetchone()[0])
            doc['lines'] = ['지원 마감: 2020-11-15']
            db.execute('UPDATE posts SET document=?', (json.dumps(doc),))
        listing = self.get('/?view=expired')
        self.assertEqual(listing.status_code, 200)
        self.assertIn('만료', listing.get_data(as_text=True))
        detail = self.get('/jobs/1?view=expired').get_data(as_text=True)
        self.assertIn('>만료</span>', detail)
        self.assertIn('11.15 마감', detail)
        self.assertNotIn('23:59', detail)
        self.assertNotIn('지원 페이지 열기', detail)
        self.assertIn('원문 보기', detail)

    def test_missing_content_and_error_views(self):
        self.assertIn("검색 결과가 없어요", self.get("/?q=없는회사").get_data(as_text=True))
        self.assertIn("아직", self.get("/?view=applied").get_data(as_text=True))
        self.assertEqual(self.get("/?page=bad").status_code, 400)
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE posts SET document='broken',reviewed_at=NULL,decision=NULL,reason=NULL")
        detail = self.get("/jobs/1").get_data(as_text=True)
        self.assertIn('class="decision-tag processing_hold">처리 보류', detail)
        self.assertIn("직무와 요건을 확인할 공고 본문이 없어요.", detail)
        self.assertIn("표시할 수 없습니다", detail)
        self.assertNotIn('data-reviewed-at="2026', detail)

    def test_state_shared_and_seen_endpoint(self):
        token = self.csrf()
        self.post("/jobs/1/state", {"status": "preparing"}, token)
        other = self.app.test_client()
        self.assertIn("Python Agent 업무", self.get("/?view=preparing", client=other).get_data(as_text=True))
        result = self.post("/jobs/1/seen", {"reviewed_at": "2026-11-16T00:00:00Z"}, token)
        self.assertEqual(result.json, {"recorded": True})
        self.assertFalse(self.post("/jobs/1/seen", {"reviewed_at": "2026-11-15T00:00:00Z"}, token).json["recorded"])

    def test_reading_text_contrast(self):
        css = (Path(dashboard.__file__).parent / "static/dashboard.css").read_text()

        def color(selector, property_name):
            block = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css).group(1)
            return re.search(r"(?:^|;)\s*" + property_name + r":\s*(#[0-9a-f]+)", block).group(1)

        def luminance(value):
            digits = value.lstrip("#")
            if len(digits) == 3:
                digits = "".join(x * 2 for x in digits)
            channels = [int(digits[i:i+2], 16) / 255 for i in (0, 2, 4)]
            linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in channels]
            return sum(x * weight for x, weight in zip(linear, (.2126, .7152, .0722)))

        for selector, background in [(".reason-preview", "#ffffff"),
                                      (".reason-preview", color(".job-row.selected", "background")),
                                      (".jd-lines", "#ffffff"), ("blockquote", "#ffffff"),
                                      (".fit-card p", color(".fit-card", "background"))]:
            with self.subTest(selector=selector, background=background):
                a, b = sorted([luminance(color(selector, "color")), luminance(background)])
                self.assertGreaterEqual((b + .05) / (a + .05), 4.5)

    def test_progress_requires_applied_and_csrf(self):
        token = self.csrf()
        payload = {'stage': 'document', 'result': 'passed', 'event_date': '2026-11-16', 'note': '서류 통과'}
        self.assertEqual(self.post('/jobs/1/progress', payload).status_code, 403)
        self.assertEqual(self.post('/jobs/1/progress', payload, token).status_code, 400)
        self.post('/jobs/1/state', {'status': 'applied'}, token)
        self.assertEqual(self.post('/jobs/1/progress', payload, token).status_code, 200)
        text = self.get('/jobs/1', client=self.app.test_client()).get_data(as_text=True)
        self.assertIn('전형 진행', text)
        self.assertEqual(text.count('class="progress-form"'), 4)
        self.assertIn('서류 통과', text)
        self.assertIn('2026-11-16', text)
        self.assertIn('서류 · 합격', self.get('/?view=applied').get_data(as_text=True))
        for bad in [None, [], {}, {**payload, 'stage': []}, {**payload, 'event_date': 'yesterday'}]:
            self.assertEqual(self.post('/jobs/1/progress', bad, token).status_code, 400)
        self.assertEqual(self.post('/jobs/999/progress', payload, token).status_code, 404)
        self.assertEqual(self.get('/jobs/1/progress').status_code, 405)

    def test_profile_signed_routes_and_safe_notes(self):
        dashboard.store.import_profile_jobs(self.source, self.state, [{'source_key': 'applications/example',
            'company': '이전기업', 'role': '개발자', 'summary': '과거 지원 자료', 'confirmed_on': '2026-11-16',
            'source_url': 'https://example.org/profile', 'document_result': 'unknown'}])
        token = self.csrf()
        text = self.get('/jobs/-1?view=applied').get_data(as_text=True)
        self.assertIn('이전기업', text)
        self.assertIn('personal-profile', text)
        self.assertNotIn('INTHISWORK', text)
        self.assertNotIn('추천 검토 미확인', text)
        self.assertNotIn('data-reviewed-at="2026', text)
        self.assertNotIn('data-status="unmarked"', text)
        self.assertEqual(self.post('/jobs/-1/state', {'status':'unmarked'}, token).status_code, 400)
        self.assertEqual(self.get('/jobs/-1', headers={}).status_code, 403)
        payload = {'stage':'interview2','result':'failed','event_date':'','note':'</textarea><script>alert(1)</script>'}
        self.assertEqual(self.post('/jobs/-1/progress', payload, token).status_code, 200)
        text = self.get('/jobs/-1?view=applied').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertEqual(self.post('/jobs/-1/state', {'status':'archived'}, token).status_code, 200)
        self.assertIn('이전기업', self.get('/?view=archived').get_data(as_text=True))
        self.assertEqual(self.get('/jobs/-999').status_code, 404)

    def test_progress_locked_state_returns_retryable_error(self):
        token = self.csrf()
        self.post('/jobs/1/state', {'status':'applied'}, token)
        with sqlite3.connect(self.state) as locked:
            locked.execute('BEGIN EXCLUSIVE')
            result = self.post('/jobs/1/progress', {'stage':'final','result':'passed','event_date':'','note':''}, token)
        self.assertEqual(result.status_code, 503)


if __name__ == "__main__":
    unittest.main()
