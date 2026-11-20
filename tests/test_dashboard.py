import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs
from job_search import dashboard
from tests.test_collect_jobs import posting


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
        self.assertIn('aria-label="마감 정보 다시 확인"', body)
        self.assertIn('<dialog id="deadline-dialog"', body)
        self.assertIn('name="q" value="Agent"', body)

    def test_imported_application_can_edit_deadline_but_cannot_refresh_source(self):
        dashboard.store.import_profile_jobs(self.source, self.state, [{
            "source_key": "applications/legacy", "company": "회사", "role": "직무", "summary": "기존 지원",
            "confirmed_on": "2026-11-15", "source_url": "https://inthiswork.com/archives/1",
        }])
        token = self.csrf()
        self.assertEqual(self.post("/jobs/-1/deadline", {"mode": "manual", "kind": "unknown"}, token).status_code, 200)
        self.assertIsNotNone(dashboard.store.get_job(self.source, self.state, -1)["deadline_manual"])
        self.assertEqual(self.post("/jobs/-1/deadline/refresh", {}, token).status_code, 400)
        self.assertNotIn('id="deadline-refresh"', self.get("/jobs/-1").get_data(as_text=True))

    def test_failed_source_request_leaves_existing_manual_value_and_memo(self):
        token = self.csrf()
        self.post("/jobs/1/state", {"status": "applied"}, token)
        self.post("/jobs/1/progress", {"stage": "interview1", "result": "in_progress", "note": "keep memo"}, token)
        self.post("/jobs/1/deadline", {"mode": "manual", "kind": "rolling", "word": "상시 채용"}, token)
        with patch.object(dashboard, "refresh_one", side_effect=dashboard.SourceCheckFailed("마지막 확인값을 유지했어요.")):
            response = self.post("/jobs/1/deadline/refresh", {}, token)
        self.assertEqual(response.status_code, 502)
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
        self.assertIn("검토 대기", detail)
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
