import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from job_search import collect_jobs, company_logos, dashboard, dashboard_store
from tests.test_collect_jobs import posting
from tests.test_deadline_sources import next_page


def logo_png(color="green"):
    buffer = io.BytesIO()
    with Image.new("RGBA", (48, 36), color=color) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def branded_post(post_id, company="Acme", content=None):
    result = posting(post_id, content)
    result["title"]["rendered"] = company + "｜엔지니어"
    return result


class LogoTests(unittest.TestCase):
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    acme_image = "https://assets.example.com/acme.png"
    acme_page = "https://careers.acme.com/jobs/eng"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.sqlite"
        self.state = Path(self.temp.name) / "state.sqlite"
        self.calls = []

    def collect(self, records, **kwargs):
        return collect_jobs.collect(self.jobs, lambda _: (records, 1), self.now, **kwargs)

    def body_logo(self, post_id, url=None):
        return branded_post(post_id, content='<img alt="Acme 공식 로고" src="' + (url or self.acme_image) + '">'
                            '<a href="' + self.acme_page + '">지원하러 가기</a>')

    def fetch(self, url, **_):
        self.calls.append(url)
        if url == self.acme_page:
            body = '<h1>Acme careers</h1><img alt="Acme 로고" src="' + self.acme_image + '">'
            return url, body.encode(), "text/html"
        self.assertEqual(url, self.acme_image)
        return url, logo_png(), "multipart/form-data"

    def assert_logo(self, post_id):
        png = company_logos.read_logo(self.jobs, post_id)
        self.assertIsNotNone(png)
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (48, 36))

    def test_body_logo_avoids_page_fetch_and_same_company_reuses_verified_copy(self):
        def after_commit(url, **options):
            with closing(sqlite3.connect(self.jobs)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM posts").fetchone()[0], 2)
            return self.fetch(url, **options)
        self.collect([self.body_logo(1), self.body_logo(2)], logo_fetch=after_commit)
        self.assertEqual(self.calls, [self.acme_image])
        self.assert_logo(1)
        self.assert_logo(2)
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM company_logos").fetchone()[0], 1)

    def test_official_page_recovers_logo_when_body_contains_only_poster_and_platform_logo(self):
        self.collect([branded_post(1, content='<p>채용 중</p><img alt="Acme 채용 포스터" src="https://x.example/poster.png">'
                                   '<img alt="Inthiswork logo" src="https://x.example/publisher.png">'
                                   '<a href="' + self.acme_page + '">지원하러가기</a>')])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision='recommend', reason='fit', reviewed_at='2026-11-19'")
            before = db.execute("SELECT * FROM posts").fetchall()
            outbox = db.execute("SELECT * FROM outbox").fetchall()
        company_logos.refresh_logos(self.jobs, limit=1, fetch=self.fetch)
        self.assertEqual(self.calls, [self.acme_page, self.acme_image])
        self.assert_logo(1)
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertEqual(db.execute("SELECT * FROM posts").fetchall(), before)
            self.assertEqual(db.execute("SELECT * FROM outbox").fetchall(), outbox)

    def test_failed_logo_retries_after_processing_other_pending_companies(self):
        seen = []
        failing = True
        first, second = "https://assets.example.com/1.png", "https://assets.example.com/2.png"
        self.collect([branded_post(1, content=f'<img alt="Acme 로고" src="{first}">'),
                      branded_post(2, "Other", f'<img alt="Other 로고" src="{second}">')])
        def fetch(url, **_):
            seen.append(url)
            if failing and url == first:
                raise TimeoutError("temporary")
            return url, logo_png(), "image/png"
        for _ in range(2):
            company_logos.refresh_logos(self.jobs, limit=1, fetch=fetch)
        self.assertEqual(seen, [first, second])
        self.assertIsNone(company_logos.read_logo(self.jobs, 1))
        self.assert_logo(2)
        failing = False
        company_logos.refresh_logos(self.jobs, limit=1, fetch=fetch)
        self.assert_logo(1)
        company_logos.refresh_logos(self.jobs, limit=1, fetch=fetch)
        self.assertEqual(seen, [first, second, first])

    def test_invalid_bytes_leave_default_avatar_and_do_not_starve_next_post(self):
        self.collect([branded_post(1, content=f'<img alt="Acme 로고" src="{self.acme_image}">'),
                      branded_post(2, content=f'<img alt="Acme 로고" src="{self.acme_image}">')])
        attempts = []
        def fetch(url, **_):
            attempts.append(url)
            return url, b"\x89PNG\r\n\x1a\ninvalid" if len(attempts) == 1 else logo_png(), "image/png"
        company_logos.refresh_logos(self.jobs, limit=1, fetch=fetch)
        company_logos.refresh_logos(self.jobs, limit=1, fetch=fetch)
        self.assertIsNone(company_logos.read_logo(self.jobs, 1))
        self.assert_logo(2)

    def test_temporary_failure_keeps_verified_logo_on_same_company_post_revision(self):
        self.collect([self.body_logo(1)], logo_fetch=self.fetch)
        saved = company_logos.read_logo(self.jobs, 1)
        new = "https://assets.example.com/updated.png"
        def failed(url, **_):
            if url == new or url == self.acme_page:
                raise TimeoutError("temporary")
            self.fail(f"Unexpected request: {url}")
        self.collect([self.body_logo(1, new)], logo_fetch=failed)
        self.assertEqual(company_logos.read_logo(self.jobs, 1), saved)

    def test_recollection_during_download_cannot_link_old_logo_to_different_company(self):
        self.collect([branded_post(1, content=f'<img alt="Acme 로고" src="{self.acme_image}">')])
        def fetch(url, **_):
            self.collect([branded_post(1, "Different", "<p>회사 변경</p>")])
            return url, logo_png(), "image/png"
        company_logos.refresh_logos(self.jobs, limit=1, fetch=fetch)
        self.assertIsNone(company_logos.read_logo(self.jobs, 1))
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["title"], "Different｜엔지니어")

    def test_public_platform_logo_requires_same_hiring_company(self):
        url = "https://www.wanted.co.kr/wd/123"
        self.collect([branded_post(1, content=f'<a href="{url}">지원하러가기</a>')])
        body = next_page({"initialData": {"id": 123, "company": {"company_id": 12, "company_name": "Different",
                          "logo_image": self.acme_image}, "position": "엔지니어", "status": "active"}})
        def fetch(requested, **_):
            self.assertEqual(requested, url)
            return url, body.encode(), "text/html"
        company_logos.refresh_logos(self.jobs, limit=1, fetch=fetch)
        self.assertIsNone(company_logos.read_logo(self.jobs, 1))

    def test_dashboard_serves_only_verified_logo_on_authenticated_local_path(self):
        self.collect([self.body_logo(1), branded_post(2, "Other", "<p>본문</p>")], logo_fetch=self.fetch)
        dashboard_store.init_state_db(self.jobs, self.state)
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision='recommend'")
        app = dashboard.create_app({
            "TESTING": True, "JOBS_DB": self.jobs, "STATE_DB": self.state, "BASE_URL": "https://dashboard.example",
            "ALLOWED_LOGIN": "owner@example.com", "SECRET_KEY": "test-secret-key-long-enough-123456",
        })
        client = app.test_client()
        headers = {"Tailscale-User-Login": "owner@example.com"}
        base = "https://dashboard.example"
        body = client.get("/", base_url=base, headers=headers).get_data(as_text=True)
        self.assertIn('src="/jobs/1/logo"', body)
        self.assertIn('aria-hidden="true">O</span>', body)
        self.assertNotIn(self.acme_image, body)
        self.assertEqual(client.get("/jobs/1/logo", base_url=base).status_code, 403)
        result = client.get("/jobs/1/logo", base_url=base, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.mimetype, "image/png")
        self.assertEqual(client.get("/jobs/2/logo", base_url=base, headers=headers).status_code, 404)

    def test_romanized_career_domain_requires_matching_structured_organization_and_job(self):
        url = "https://careers.hanbit.example/jobs/engineer"
        body = f'<a href="{url}">지원하러가기</a>'
        self.collect([branded_post(1, "한빛전자", body), branded_post(2, "한빛물류", body)])
        page = '<script type="application/ld+json">' + json.dumps({
            "@type": "JobPosting", "title": "엔지니어", "hiringOrganization": {
                "@type": "Organization", "name": "한빛전자", "logo": self.acme_image,
            },
        }) + '</script><img alt="한빛물류 로고" src="https://unrelated.example/other.png">'
        seen = []
        def fetch(requested, **_):
            seen.append(requested)
            return requested, page.encode() if requested == url else logo_png(), "text/html"
        company_logos.refresh_logos(self.jobs, limit=2, fetch=fetch)
        self.assertEqual(seen, [url, self.acme_image, url])
        self.assert_logo(1)
        self.assertIsNone(company_logos.read_logo(self.jobs, 2))
