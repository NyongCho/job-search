import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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

    def collect(self, records):
        return collect_jobs.collect(self.jobs, lambda _: (records, 1), self.now)

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
        self.collect([self.body_logo(1), self.body_logo(2)])
        company_logos.refresh_logos(self.jobs, fetch=after_commit)
        self.assertEqual(self.calls, [self.acme_image])
        self.assert_logo(1)
        self.assert_logo(2)
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM company_logos").fetchone()[0], 1)
            query = "SELECT outcome, attempted_at FROM job_company_logos WHERE post_id=1"
            outcome, attempted_at = db.execute(query).fetchone()
            self.assertEqual(outcome, "ready")
            self.assertIsNotNone(attempted_at)
            self.assertEqual(self.collect([self.body_logo(1)])["changed"], 0)
            self.assertEqual(db.execute(query).fetchone(), ("ready", attempted_at))

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
        self.collect([self.body_logo(1)])
        company_logos.refresh_logos(self.jobs, fetch=self.fetch)
        saved = company_logos.read_logo(self.jobs, 1)
        new = "https://assets.example.com/updated.png"
        def failed(url, **_):
            if url == new or url == self.acme_page:
                raise TimeoutError("temporary")
            self.fail(f"Unexpected request: {url}")
        self.collect([self.body_logo(1, new)])
        company_logos.refresh_logos(self.jobs, fetch=failed)
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
        self.collect([self.body_logo(1), branded_post(2, "Other", "<p>본문</p>")])
        company_logos.refresh_logos(self.jobs, fetch=self.fetch)
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

    def test_drain_processes_53_jobs_and_retries_timeout_once_per_round(self):
        timeout_url = "https://assets.example.com/timeout.png"
        posts = [branded_post(1, content=f'<img alt="Acme 로고" src="{timeout_url}">')]
        posts.extend(self.body_logo(i) for i in range(2, 54))
        self.collect(posts)
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("DELETE FROM job_company_logos WHERE post_id > 10")

        def fetch(url, **options):
            if url == timeout_url:
                self.calls.append(url)
                raise TimeoutError("temporary")
            return self.fetch(url, **options)

        cutoff = time.time()
        self.assertEqual(company_logos.drain_logos(self.jobs, retry_before=cutoff, fetch=fetch),
                         {"checked": 53, "ready": 52, "missing": 0, "retry": 1})
        self.assertEqual(self.calls, [timeout_url, self.acme_image])
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_company_logos").fetchone()[0], 53)
            outcome, attempted_at, lease_until = db.execute(
                "SELECT outcome, attempted_at, lease_until FROM job_company_logos WHERE post_id=1"
            ).fetchone()
            self.assertEqual((outcome, lease_until), ("pending", 0))
            self.assertGreaterEqual(attempted_at, cutoff)

        self.calls.clear()
        self.assertEqual(company_logos.drain_logos(self.jobs, retry_before=cutoff, fetch=fetch)["checked"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(company_logos.drain_logos(self.jobs, retry_before=attempted_at + 1e-6, fetch=fetch)["retry"], 1)
        self.assertEqual(self.calls, [timeout_url])

    def test_drain_seeds_past_batch_without_logo_clues(self):
        self.collect([branded_post(i, content="<p>공고</p>") for i in range(1, 21)] + [self.body_logo(21)])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("DELETE FROM job_company_logos")
        company_logos.drain_logos(self.jobs, retry_before=time.time(), fetch=self.fetch)
        self.assertEqual(self.calls, [self.acme_image])
        self.assert_logo(21)
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_company_logos WHERE outcome='missing'").fetchone()[0], 20)

    def test_drain_waits_for_interrupted_claim_from_same_round(self):
        self.collect([self.body_logo(1)])
        cutoff = time.time()
        claimed = cutoff + 1
        lease = cutoff + 15
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE job_company_logos SET attempted_at=?, lease_until=?", (claimed, lease))
        clock = claimed

        def wait(seconds):
            nonlocal clock
            self.assertGreater(seconds, 0)
            clock += seconds

        with patch.object(company_logos.time, "time", side_effect=lambda: clock), \
             patch.object(company_logos.time, "sleep", side_effect=wait):
            company_logos.drain_logos(self.jobs, retry_before=cutoff, fetch=self.fetch)
        self.assertEqual(clock, lease)
        self.assertEqual(self.calls, [self.acme_image])
        self.assert_logo(1)

    def test_drain_detects_company_changed_during_download(self):
        replacement = "https://assets.example.com/different.png"
        self.collect([self.body_logo(1)])

        def fetch(url, **_):
            self.calls.append(url)
            if url == self.acme_image:
                self.collect([branded_post(1, "Different", f'<img alt="Different logo" src="{replacement}">')])
            return url, logo_png(), "image/png"

        result = company_logos.drain_logos(self.jobs, limit=1, retry_before=time.time(), fetch=fetch)
        self.assertEqual(result["ready"], 1)
        self.assertEqual(self.calls, [self.acme_image, replacement])
        self.assert_logo(1)
        with closing(sqlite3.connect(self.jobs)) as db:
            self.assertEqual(db.execute("SELECT image_url FROM company_logos").fetchall(), [(replacement,)])

    def test_drain_reconciles_inconsistent_existing_queue_rows(self):
        self.collect([self.body_logo(1), self.body_logo(2)])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("DELETE FROM posts WHERE id=1")
            db.execute("UPDATE job_company_logos SET document_version='stale' WHERE post_id=2")
        with patch.object(company_logos.time, "sleep", side_effect=AssertionError("unexpected wait")):
            company_logos.drain_logos(self.jobs, limit=1, retry_before=time.time(), fetch=self.fetch)
        self.assertEqual(self.calls, [self.acme_image])
        self.assert_logo(2)

    def test_drain_fails_when_required_table_is_missing(self):
        self.collect([])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("DROP TABLE company_logos")
        with self.assertRaisesRegex(sqlite3.OperationalError, "company_logos"):
            company_logos.drain_logos(self.jobs, retry_before=time.time(), fetch=self.fetch)

    def test_drain_fails_when_pillow_is_missing(self):
        self.collect([])
        with patch.dict(sys.modules, {"PIL": None}):
            with self.assertRaises(ImportError):
                company_logos.drain_logos(self.jobs, retry_before=time.time(), fetch=self.fetch)

    def test_cli_limit_is_batch_size_only_with_drain(self):
        self.collect([self.body_logo(1), self.body_logo(2)])
        args = ["company_logos", "--db", str(self.jobs), "--limit", "1"]
        with patch.object(company_logos, "fetch_public", self.fetch):
            with patch.object(sys, "argv", args), redirect_stdout(io.StringIO()) as output:
                company_logos.main()
            self.assertEqual(json.loads(output.getvalue())["checked"], 1)
            self.collect([self.body_logo(3)])
            with patch.object(sys, "argv", args + ["--drain"]), redirect_stdout(io.StringIO()) as output:
                company_logos.main()
            self.assertEqual(json.loads(output.getvalue())["checked"], 2)

    def run_drain_cli(self, fetch=None):
        args = ["company_logos", "--db", str(self.jobs), "--drain"]
        with patch.object(sys, "argv", args), patch.object(company_logos, "fetch_public", fetch or self.fetch), \
             redirect_stdout(io.StringIO()) as output:
            company_logos.main()
        return json.loads(output.getvalue())

    def test_module_and_manual_drain_work_without_posix_signal_support(self):
        self.collect([])
        code = """import os, sys
sys.modules["fcntl"] = None
if hasattr(os, "O_DIRECTORY"):
    del os.O_DIRECTORY
from job_search import collect_jobs, company_logos
sys.argv = ["company_logos", "--db", sys.argv[1], "--drain"]
company_logos.main()
"""
        process = subprocess.run([sys.executable, "-c", code, self.jobs], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["checked"], 0)

    def test_drain_preserves_signal_published_during_last_download(self):
        requests = self.jobs.with_suffix(".sqlite.logo-requests")
        requests.mkdir()
        self.collect([self.body_logo(1)])
        first_signals = set(requests.iterdir())
        self.assertEqual(len(first_signals), 1)

        def fetch(url, **options):
            self.collect([])
            return self.fetch(url, **options)

        self.assertEqual(self.run_drain_cli(fetch)["ready"], 1)
        remaining = set(requests.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertFalse(first_signals & remaining)
        self.assertEqual(self.run_drain_cli()["checked"], 0)
        self.assertEqual(list(requests.iterdir()), [])

    def test_drain_uses_signal_creation_time_for_retry_cutoff(self):
        requests = self.jobs.with_suffix(".sqlite.logo-requests")
        requests.mkdir()
        signal_time = time.time() - 100
        self.collect([self.body_logo(1)])
        for signal in requests.iterdir():
            signal.unlink()
        (requests / f"{int(signal_time * 1_000_000_000)}-{'0' * 32}").touch()
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE job_company_logos SET attempted_at=?", (signal_time + 1,))
        self.assertEqual(self.run_drain_cli()["checked"], 0)
        self.assertEqual(list(requests.iterdir()), [])

    def test_drain_preserves_signal_when_database_is_write_locked(self):
        requests = self.jobs.with_suffix(".sqlite.logo-requests")
        requests.mkdir()
        self.collect([self.body_logo(1)])
        self.assertEqual(len(signals := list(requests.iterdir())), 1)
        signal = signals[0]
        with closing(sqlite3.connect(self.jobs)) as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                self.run_drain_cli()
        self.assertTrue(signal.exists())
        self.assertEqual(self.run_drain_cli()["ready"], 1)
        self.assertFalse(signal.exists())

    def test_drain_preserves_signal_when_tables_or_pillow_are_missing(self):
        requests = self.jobs.with_suffix(".sqlite.logo-requests")
        requests.mkdir()
        self.collect([])
        self.assertEqual(len(signals := list(requests.iterdir())), 1)
        signal = signals[0]
        with patch.dict(sys.modules, {"PIL": None}), self.assertRaises(ImportError):
            self.run_drain_cli()
        self.assertTrue(signal.exists())
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("DROP TABLE company_logos")
        with self.assertRaisesRegex(sqlite3.OperationalError, "company_logos"):
            self.run_drain_cli()
        self.assertTrue(signal.exists())

    def test_drain_reports_unexpected_visible_entries_without_deleting_them(self):
        requests = self.jobs.with_suffix(".sqlite.logo-requests")
        requests.mkdir()
        self.collect([])
        unexpected = requests / f"123-{'0' * 32}"
        unexpected.write_text("keep this file")
        self.assertEqual(self.run_drain_cli()["checked"], 0)
        with self.assertRaisesRegex(OSError, unexpected.name):
            self.run_drain_cli()
        self.assertEqual(list(requests.iterdir()), [unexpected])
        self.assertEqual(unexpected.read_text(), "keep this file")

    def test_drain_defers_timeout_when_wall_clock_is_behind_round_cutoff(self):
        self.collect([branded_post(1, content=f'<img alt="Acme 로고" src="{self.acme_image}">')])
        now = time.time()
        cutoff = now + 30

        def timeout(url, **_):
            self.assertEqual(self.calls, [])
            self.calls.append(url)
            raise TimeoutError("temporary")

        with patch.object(company_logos.time, "time", return_value=now):
            result = company_logos.drain_logos(self.jobs, retry_before=cutoff, fetch=timeout)
        self.assertEqual(result["retry"], 1)
        self.assertEqual(self.calls, [self.acme_image])

    def test_drain_does_not_claim_a_timeout_completed_after_selection(self):
        self.collect([self.body_logo(1)])
        cutoff = time.time()
        clock_reads = 0

        def clock():
            nonlocal clock_reads
            clock_reads += 1
            if clock_reads == 2:
                with closing(sqlite3.connect(self.jobs)) as db, db:
                    db.execute("UPDATE job_company_logos SET attempted_at=?, lease_until=0", (cutoff + 1,))
            return cutoff + 2

        with patch.object(company_logos.time, "time", side_effect=clock):
            result = company_logos.drain_logos(self.jobs, retry_before=cutoff, fetch=self.fetch)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(self.calls, [])

    def test_drain_resets_the_batch_deadline(self):
        urls = [f"https://assets.example.com/{i}.png" for i in range(1, 4)]
        self.collect([branded_post(i, str(i), f'<img alt="{i} logo" src="{url}">')
                      for i, url in enumerate(urls, 1)])
        clock = 0

        def fetch(url, *, deadline, **_):
            nonlocal clock
            self.calls.append(url)
            self.assertEqual(deadline - clock, 5)
            clock += 5
            return url, logo_png(), "image/png"

        with patch.object(company_logos.time, "monotonic", side_effect=lambda: clock):
            result = company_logos.drain_logos(self.jobs, limit=1, retry_before=time.time(), fetch=fetch)
        self.assertEqual(result["ready"], 3)
        self.assertEqual(self.calls, urls)
