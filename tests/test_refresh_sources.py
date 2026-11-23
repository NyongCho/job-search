import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from job_search import collect_jobs, company_logos, dashboard, dashboard_store, refresh_queue
from tests.test_company_logos import branded_post, logo_png
from tests.test_deadline_sources import next_page
from tests.test_collect_jobs import posting


class RefreshSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.sqlite"
        self.state = Path(self.temp.name) / "state.sqlite"

    def seed(self, records):
        collect_jobs.collect(self.jobs, lambda _: (records, 1))
        dashboard_store.init_state_db(self.jobs, self.state)
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS test_events(id INTEGER PRIMARY KEY,status TEXT)")

    def task(self, post_id, **extra):
        return {
            "id": post_id,
            "post_id": post_id,
            "version": collect_jobs.get_posts(self.jobs, [post_id])[0]["fingerprint"],
            **extra,
        }

    def update_document(self, post_id, **fields):
        with closing(sqlite3.connect(self.jobs)) as db, db:
            doc = json.loads(db.execute("SELECT document FROM posts WHERE id=?", (post_id,)).fetchone()[0])
            doc.update(fields)
            db.execute("UPDATE posts SET document=? WHERE id=?", (json.dumps(doc), post_id))

    def finish(self, task, status, message="", db=None):
        if db is None:
            with closing(sqlite3.connect(self.state)) as db, db:
                self.finish(task, status, message, db=db)
            return
        db.execute("INSERT OR IGNORE INTO test_events VALUES (?,?)", (task["id"], status))

    def events(self):
        with closing(sqlite3.connect(self.state)) as db:
            return dict(db.execute("SELECT * FROM test_events"))

    def deadlines(self, tasks, fetch, finish=None):
        from job_search.refresh_sources import refresh_deadlines

        return refresh_deadlines(self.jobs, self.state, tasks, finish or self.finish, fetch=fetch)

    def logo(self, task, fetch, finish=None):
        from job_search.refresh_sources import refresh_logo_task

        return refresh_logo_task(self.jobs, self.state, task, finish or self.finish, fetch=fetch)

    def test_shared_board_is_read_once_across_worker_claims_and_again_for_a_new_click(self):
        from functools import partial
        from job_search import refresh_sources, refresh_worker
        from tests.test_deadline_sources import ascent_page

        url = "https://www.ascent.me/jobs/freepass"
        ids = range(1, 54)  # More than both concurrent claims of 25.
        self.seed([posting(i) for i in ids])
        for i in ids:
            self.update_document(i, source="ascent", source_job_id=str(i), title=f"회사｜개발자{i}", url=url)
        calls = []
        deadline = ""

        def fetch(requested, **_):
            self.assertEqual(requested, url)
            calls.append(requested)
            roles = [dict(id=i, title=f"개발자{i}", conditions=[deadline]) for i in ids]
            return url, ascent_page(roles, "회사").encode(), "text/html"

        def skip(jobs, state, tasks, finish):
            for task in tasks if isinstance(tasks, list) else [tasks]:
                finish(task, "skipped")

        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(refresh_worker.serve, self.jobs, self.state, stop,
                                 deadlines=partial(refresh_sources.refresh_deadlines, fetch=fetch),
                                 logos=skip, fits=skip, poll=0.01)
            try:
                for round_, day in enumerate(("20", "21"), start=1):
                    deadline = f"2099.12.{day} 마감"
                    targets = [(i, self.task(i)["version"]) for i in ids]
                    run = refresh_queue.enqueue(self.state, targets, "list:recommended:")
                    until = time.monotonic() + 5
                    while not (status := refresh_queue.snapshot(self.state, run))["complete"]:
                        self.assertLess(time.monotonic(), until)
                        time.sleep(0.01)
                    self.assertEqual(status["counts"]["done"], len(ids))
                    self.assertEqual(len(calls), round_)
                    for post_id in (1, 26, 53):
                        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, post_id)["deadline_label"],
                                         f"12.{day} 마감")
            finally:
                stop.set()
                worker.result(5)

    def test_include_validates_duplicates_and_commits_before_next_response(self):
        self.seed([posting(i) for i in range(1, 102)])
        tasks = [self.task(i) for i in range(1, 102)]
        calls = []
        second_started, release_second = threading.Event(), threading.Event()

        def fetch(url, **_):
            ids = list(map(int, parse_qs(urlsplit(url).query)["include"][0].split(",")))
            calls.append(ids)
            if 1 in ids:
                result = [posting(i, "<p>상시 채용</p>") for i in ids if i != 4]
                result += [posting(2), posting(999)]
                result[2]["title"]["rendered"] = "다른 기업｜엔지니어"
                return url, json.dumps(result).encode(), "application/json"
            second_started.set()
            self.assertTrue(release_second.wait(4))
            raise TimeoutError()

        with ThreadPoolExecutor() as pool:
            refreshing = pool.submit(self.deadlines, tasks, fetch)
            try:
                self.assertTrue(second_started.wait(3))
                end = time.monotonic() + 2
                while 4 not in self.events() and time.monotonic() < end:
                    time.sleep(0.01)
                events = self.events()
                self.assertEqual([events[i] for i in range(1, 5)], ["done", "failed", "failed", "failed"])
                self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["deadline_label"], "상시 채용")
            finally:
                release_second.set()
            refreshing.result(3)
        self.assertEqual(sorted(map(len, calls)), [1, 100])
        self.assertEqual(self.events()[101], "failed")

    def test_newer_jd_and_logo_are_from_the_same_inthiswork_response(self):
        old = posting(1)
        self.seed([old])
        task = self.task(1)
        logo_task = dict(task, id=2, source_work_id=1, source_status="running")
        asset = "https://assets.example.com/verified.png"
        fresh = posting(1, '<p>필수 경력 8년</p><p>상시 채용</p><img alt="회사 로고" src="' + asset + '">')
        fresh["modified_gmt"] = "2026-11-21T00:00:00"
        seen = []

        def fetch(url, **_):
            seen.append(url)
            if url == asset:
                return url, logo_png(), "image/png"
            self.assertTrue(url.startswith(collect_jobs.API + "?"))
            return url, json.dumps([fresh]).encode(), "application/json"

        self.assertFalse(self.logo(logo_task, fetch))
        self.deadlines([task], fetch)
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["lines"], ["필수 경력 8년", "상시 채용"])

        def crash(*_, **kw):
            raise RuntimeError("restart")

        with self.assertRaisesRegex(RuntimeError, "restart"):
            self.logo(logo_task, fetch, crash)
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))
        self.assertTrue(self.logo(logo_task, fetch))
        self.assertEqual(self.events(), {1: "done", 2: "done"})
        self.assertEqual(len(seen), 2)

    def test_verified_cache_finishes_while_deadline_is_running_and_workers_share_image_claim(self):
        url = "https://assets.example.com/acme.png"
        self.seed([branded_post(i, content=f'<img alt="Acme 로고" src="{url}">') for i in (1, 2, 3)])
        started, release = threading.Event(), threading.Event()
        calls = []

        def fetch(actual, **_):
            calls.append(actual)
            self.assertEqual(url, actual)
            started.set()
            self.assertTrue(release.wait(3))
            return url, logo_png(), "image/png"

        with ThreadPoolExecutor() as pool:
            legacy = pool.submit(company_logos.refresh_logos, self.jobs, limit=1, fetch=fetch)
            self.assertTrue(started.wait(2))
            manual = pool.submit(self.logo, self.task(2), fetch)
            try:
                end = time.monotonic() + 2
                with closing(sqlite3.connect(self.jobs)) as db:
                    while not db.execute("SELECT lease_until FROM job_company_logos WHERE post_id=2").fetchone()[
                        0
                    ]:
                        self.assertLess(time.monotonic(), end, "manual worker did not claim the other posting")
                        time.sleep(0.01)
            finally:
                release.set()
            legacy.result(3)
            self.assertTrue(manual.result(3))
        self.assertTrue(self.logo(self.task(3, source_work_id=99, source_status="running"), fetch))
        self.assertEqual(calls, [url])
        for post_id in [1, 2, 3]:
            self.assertIsNotNone(company_logos.read_logo(self.jobs, post_id))

    def test_verified_external_page_shares_original_job_and_preserves_body_on_deadline_only(self):
        self.seed([posting(1)])
        url = "https://www.wanted.co.kr/wd/123"
        image = "https://assets.example.com/wanted-company.png"
        self.update_document(1, source="wanted", source_job_id="123", url=url, lines=[])
        data = {
            "id": 123,
            "company": {"company_id": 3, "company_name": "회사", "logo_image": image},
            "position": "엔지니어",
            "status": "active",
            "due_time": "2026-12-14",
            "requirements": "<p>Python 5년</p>",
        }
        calls = []

        def fetch(requested, **_):
            calls.append(requested)
            if requested == image:
                return requested, logo_png(), "image/png"
            self.assertEqual(requested, url)
            return requested, next_page({"initialData": data}).encode(), "text/html"

        task = self.task(1)
        self.deadlines([task], fetch)
        self.assertTrue(self.logo(dict(task, id=2, source_work_id=1, source_status="done"), fetch))
        self.assertEqual(calls, [url, image])
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["lines"], ["Python 5년"])
        data.pop("requirements")
        self.deadlines([self.task(1, id=3)], fetch)
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["lines"], ["Python 5년"])
        self.assertEqual(self.events()[3], "done")

    def test_refresh_reads_linked_verified_jd_and_logo_when_the_listing_has_only_an_apply_link(self):
        url = "https://www.wanted.co.kr/wd/321"
        image = "https://assets.example.com/company-321.png"
        record = posting(1, f'<p>마감일</p><p>2026-12-31</p><a href="{url}">지원하러가기</a>')
        self.seed([record])
        calls = []

        def fetch(requested, **_):
            calls.append(requested)
            if requested == url:
                return (
                    url,
                    next_page(
                        {
                            "initialData": {
                                "id": 321,
                                "company": {"company_id": 8, "company_name": "회사", "logo_image": image},
                                "position": "엔지니어",
                                "status": "active",
                                "requirements": "<p>필수 경력 3년</p>",
                                "preferred_points": "<p>공공 데이터 경험 우대</p>",
                            }
                        }
                    ).encode(),
                    "text/html",
                )
            if requested == image:
                return image, logo_png(), "image/png"
            self.assertTrue(requested.startswith(collect_jobs.API + "?"))
            return requested, json.dumps([record]).encode(), "application/json"

        task = self.task(1)
        self.deadlines([task], fetch)
        self.assertTrue(self.logo(dict(task, id=2, source_work_id=1, source_status="done"), fetch))
        detail = collect_jobs.get_posts(self.jobs, [1])[0]
        self.assertEqual(detail["lines"], ["필수 경력 3년", "공공 데이터 경험 우대"])
        self.assertEqual(detail["jd_source"], {"source": "wanted", "url": url, "original_id": "321"})
        self.assertEqual(calls[1:], [url, image])
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["deadline_label"], "12.31 마감")

    def test_changed_document_and_failed_event_preserve_the_last_source_facts_and_evidence(self):
        self.seed([posting(1)])
        task = self.task(1)

        def first(url, **_):
            return url, json.dumps([posting(1, "<p>모집 마감 2099-12-31</p>")]).encode(), "application/json"

        self.deadlines([task], first)
        saved = dashboard_store.get_job(self.jobs, self.state, 1)
        newer = posting(1, "<p>필수 경력 7년</p><p>상시 채용</p>")
        newer["modified_gmt"] = "2026-11-22T00:00:00"

        def commit_error(task, status, message="", db=None):
            self.finish(task, status, message, db)
            raise RuntimeError("transaction interrupted")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.deadlines(
                [dict(task, id=2)], lambda url, **kw: (url, json.dumps([newer]).encode(), ""), commit_error
            )
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["lines"], saved["lines"])
        self.assertEqual(
            dashboard_store.get_job(self.jobs, self.state, 1)["availability_checked_at"],
            saved["availability_checked_at"],
        )
        self.assertNotIn(2, self.events())

        def swap(url, **_):
            updated = posting(1, "<p>새 JD 10년</p>")
            updated["modified_gmt"] = "2026-11-23T00:00:00"
            collect_jobs.collect(self.jobs, lambda _: ([updated], 1))
            return url, json.dumps([newer]).encode(), "application/json"

        self.deadlines([dict(task, id=3)], swap)
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["lines"], ["새 JD 10년"])
        self.assertEqual(self.events()[3], "skipped")

    def test_an_older_automatic_source_cannot_replace_a_verified_jd(self):
        self.seed([posting(1)])
        newer = posting(1, "<p>경력 5년</p><p>모집 마감일 2099-12-31</p>")
        newer["modified_gmt"] = "2026-11-22T00:00:00"

        def response(record):
            return lambda url, **kw: (url, json.dumps([record]).encode(), "application/json")

        self.deadlines([self.task(1)], response(newer))
        saved = dashboard_store.get_job(self.jobs, self.state, 1)
        older = posting(1, "<p>경력 1년</p><p>상시 채용</p>")
        older["modified_gmt"] = "2026-11-21T00:00:00"
        self.deadlines([self.task(1, id=2)], response(older))
        current = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(current["lines"], ["경력 5년", "모집 마감일 2099-12-31"])
        self.assertEqual(current["deadline_label"], "12.31 마감")
        self.assertEqual(current["availability_checked_at"], saved["availability_checked_at"])

    def test_slow_official_post_cannot_hold_up_a_faster_role_on_the_same_claim(self):
        self.seed([posting(1), posting(2)])
        for i in (1, 2):
            self.update_document(i, source="wanted", source_job_id=str(i), url=f"https://www.wanted.co.kr/wd/{i}")
        slow_entered, release = threading.Event(), threading.Event()
        calls = []

        class FixtureSource(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                i = int(self.path.rsplit("/", 1)[1])
                if i == 1:
                    slow_entered.set()
                    release.wait(5)
                else:
                    slow_entered.wait(2)
                page = next_page(
                    {
                        "initialData": {
                            "id": i,
                            "company": {"company_id": 1, "company_name": "회사"},
                            "position": "엔지니어",
                            "status": "active",
                            "due_time": "2026-12-11",
                        }
                    }
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(page.encode())

            def log_message(self, format, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), FixtureSource) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                client = dashboard.create_app(
                    {
                        "JOBS_DB": self.jobs,
                        "STATE_DB": self.state,
                        "DEV_MODE": True,
                        "BASE_URL": "http://localhost",
                        "SECRET_KEY": "x" * 40,
                    }
                ).test_client()
                run = refresh_queue.enqueue(
                    self.state, [(i, self.task(i)["version"]) for i in (1, 2)], "list:test"
                )
                tasks = refresh_queue.claim(self.state, "deadline", 2)
                ready = threading.Event()

                def finished(task, status, message="", db=None):
                    refresh_queue.finish(self.state, task, status, message, db=db)
                    if task["post_id"] == 2:
                        ready.set()

                def fetch(url, **_):
                    local = f"http://127.0.0.1:{server.server_port}" + urlsplit(url).path
                    with urlopen(local, timeout=5) as response:
                        return url, response.read(), "text/html"

                with ThreadPoolExecutor() as pool:
                    work = pool.submit(self.deadlines, tasks, fetch, finished)
                    try:
                        self.assertTrue(ready.wait(3))
                        end = time.monotonic() + 2
                        while not (view := client.get(f"/refreshes/{run}?after=0").get_json())["changes"]:
                            self.assertLess(time.monotonic(), end, "first event did not commit")
                            time.sleep(0.01)
                        self.assertEqual([(c["post_id"], c["status"]) for c in view["changes"]], [(2, "done")])
                        self.assertEqual(
                            dashboard_store.get_job(self.jobs, self.state, 2)["deadline_label"], "12.11 마감"
                        )
                    finally:
                        release.set()
                    work.result(3)
                final = client.get(f"/refreshes/{run}?after={view['cursor']}").get_json()
                self.assertEqual([(c["post_id"], c["status"]) for c in final["changes"]], [(1, "done")])
                self.assertCountEqual(calls, ["/wd/1", "/wd/2"])
            finally:
                release.set()
                server.shutdown()
                thread.join(3)



    def test_expired_linked_greeting_job_keeps_employer_logo_fallback_in_snapshot(self):
        from job_search.public_fetch import PublicHTTPError

        host = "siliconii.career.greetinghr.com"
        home = f"https://{host}/"
        expired = home + "ko/o/239254"
        image = "https://profiles.greetinghr.com/group/logo"
        listing = branded_post(1, "Silicon2", f'<a href="{expired}">지원하러가기</a>')
        self.seed([listing])
        board = next_page({"host": host, "careerBaseInfo": {
            "workspace": {"id": 5, "name": "(주) 실리콘투", "imageUrl": image}},
            "careerBootInfo": {"design": {"footer": {"companyName": "Silicon2"}}}})
        urls = []
        def fetch(url, **_):
            urls.append(url)
            if url.startswith(collect_jobs.API):
                return url, json.dumps([listing]).encode(), "application/json"
            if url == expired:
                raise PublicHTTPError(404)
            if url == home:
                return home + "ko/home", board.encode(), "text/html"
            self.assertEqual(url, image)
            return url, logo_png(), "image/png"

        task = self.task(1)
        self.deadlines([task], fetch)
        self.assertEqual(self.events(), {1: "failed"})
        with closing(sqlite3.connect(self.jobs)) as db, db:
            row = db.execute("SELECT logo_plan FROM refresh_source_snapshots WHERE work_id=1").fetchone()
            self.assertEqual(json.loads(row[0])["page"], home)
            # Existing terminal snapshots contain the same verified locator with a null page.
            db.execute("UPDATE refresh_source_snapshots SET logo_plan=json_set(logo_plan,'$.page',NULL) WHERE work_id=1")
        self.logo(dict(task, id=2, source_work_id=1, source_status="failed"), fetch)
        self.assertEqual(self.events(), {1: "failed", 2: "done"})
        self.assertEqual(urls[1:], [expired, home, image])
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))

    def test_failed_greeting_deadline_still_refreshes_the_employers_board_logo(self):
        host = "acme.career.greetinghr.com"
        url = f"https://{host}/ko/o/1"
        asset = "https://profiles.greetinghr.com/group/acme"
        self.seed([branded_post(1)])
        self.update_document(1, source="greeting", source_job_id=1, url=url)
        board = next_page({"host": host, "careerBaseInfo": {
            "workspace": {"id": 8, "name": "Acme", "imageUrl": asset}}})
        calls = []
        def fetch(requested, **_):
            calls.append(requested)
            if requested == f"https://{host}/":
                return f"https://{host}/ko/intro", board.encode(), "text/html"
            self.assertEqual(requested, asset)
            return asset, logo_png(), "image/png"

        self.logo(self.task(1, id=2, source_work_id=1, source_status="failed"), fetch)
        self.assertEqual(self.events(), {2: "done"})
        self.assertEqual(calls, [f"https://{host}/", asset])
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))

    def test_invalid_deadline_does_not_discard_an_independently_verified_logo(self):
        self.seed([posting(1)])
        image = "https://assets.example.com/logo.png"
        url = "https://www.wanted.co.kr/wd/321"
        self.update_document(1, source="wanted", source_job_id=321, url=url)
        calls = []

        def fetch(requested, **_):
            calls.append(requested)
            if requested == image:
                return image, logo_png(), "image/png"
            self.assertEqual(requested, url)
            return (
                url,
                next_page(
                    {
                        "initialData": {
                            "id": 321,
                            "company": {"company_id": 1, "company_name": "회사", "logo_image": image},
                            "position": "엔지니어",
                            "status": "active",
                            "due_time": "invalid",
                        }
                    }
                ).encode(),
                "text/html",
            )

        task = self.task(1)
        self.deadlines([task], fetch)
        self.logo(dict(task, id=2, source_work_id=1, source_status="failed"), fetch)
        self.assertEqual(self.events(), {1: "failed", 2: "done"})
        self.assertEqual(calls, [url, image])
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))

    def test_shared_board_response_is_checked_against_each_requested_role(self):
        from tests.test_deadline_sources import SourceRefreshTests, ascent_page

        jobs = [SourceRefreshTests.fde, SourceRefreshTests.staff]
        url = SourceRefreshTests.ascent_url
        self.seed([posting(1), posting(2)])
        for i, role in enumerate(jobs, 1):
            self.update_document(
                i, source="ascent", source_job_id=role["id"], title="래티스(Lattice)｜" + role["title"], url=url
            )
        calls = []

        def fetch(requested, **_):
            calls.append(requested)
            return requested, ascent_page([*jobs, jobs[0]]).encode(), "text/html"

        self.deadlines([self.task(1), self.task(2)], fetch)
        self.assertEqual(calls, [url])
        self.assertEqual(self.events(), {1: "failed", 2: "done"})
        self.assertIsNone(dashboard_store.get_job(self.jobs, self.state, 1)["availability_checked_at"])
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 2)["deadline_label"], "12.15 마감")

    def test_same_official_revision_cannot_replace_previously_verified_evidence(self):
        self.seed([posting(1)])
        url = "https://job-boards.greenhouse.io/example/jobs/123"
        self.update_document(1, source="greenhouse", source_job_id=123, url=url)
        response = {
            "id": 123,
            "company_name": "회사",
            "title": "엔지니어",
            "absolute_url": url,
            "application_deadline": "2099-12-31",
            "content": "<p>필수 경력 5년</p>",
            "updated_at": "2026-11-22T11:00:00Z",
        }

        def fetch(url, **_):
            return url, json.dumps(response).encode(), "application/json"

        self.deadlines([self.task(1)], fetch)
        response["content"] = "<p>필수 경력 1년</p>"
        self.deadlines([self.task(1, id=2)], fetch)
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["lines"], ["필수 경력 5년"])

    def test_official_revision_cannot_disappear_after_a_timestamped_success(self):
        from job_search.fit_work import verify_openings

        self.seed([posting(1)])
        url = "https://job-boards.greenhouse.io/example/jobs/19"
        self.update_document(1, source="greenhouse", source_job_id=19, url=url)
        role = {"id": 19, "company_name": "회사", "title": "엔지니어", "absolute_url": url,
                "updated_at": "2026-11-23T01:00:00Z", "content": "<p>필수 경력 5년</p>",
                "application_deadline": "2099-12-31"}

        def fetch(requested, **_):
            return requested, json.dumps(role).encode(), "application/json"

        self.deadlines([self.task(1)], fetch)
        saved = dashboard_store.get_job(self.jobs, self.state, 1)
        del role["updated_at"]
        role["application_deadline"] = "2000-01-01"
        self.deadlines([self.task(1, id=2)], fetch)
        current = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(current["deadline_label"], "12.31 마감")
        self.assertEqual(current["availability_checked_at"], saved["availability_checked_at"])
        self.assertEqual(verify_openings(collect_jobs.get_posts(self.jobs, [1]), fetch=fetch), {})

    def test_periodic_refresh_does_not_publish_against_a_replaced_document(self):
        from job_search.refresh_job_availability import refresh

        self.seed([posting(1)])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision='recommend'")

        def fetch(_):
            changed = posting(1)
            changed["title"]["rendered"] = "다른 회사｜다른 직무"
            collect_jobs.collect(self.jobs, lambda _: ([changed], 1))
            return [posting(1, "<p>지원 종료</p>")], 1

        refresh(self.jobs, self.state, fetch)
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_availability").fetchone()[0], 0)

    def test_malformed_legacy_document_cannot_block_other_checked_roles(self):
        self.seed([posting(1), posting(2)])
        self.update_document(1, url=42)
        self.deadlines(
            [self.task(1), self.task(2)],
            lambda url, **_: (url, json.dumps([posting(2)]).encode(), "application/json"),
        )
        self.assertEqual(self.events(), {1: "skipped", 2: "done"})

    def test_interrupted_company_download_is_reclaimed_without_failing_another_subscriber(self):
        from unittest.mock import patch

        url = "https://assets.example.com/acme.png"
        self.seed([branded_post(1, content=f'<img alt="Acme 로고" src="{url}">')])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            key = db.execute("SELECT identity FROM job_company_logos WHERE post_id=1").fetchone()[0]
            db.execute(
                "INSERT INTO logo_image_claims VALUES(?,?,?,?)", (key, url, "interrupted", time.time() + 15)
            )
        task = self.task(1)
        with patch.object(company_logos.time, "monotonic", side_effect=[100.0, 110.0]):
            self.assertFalse(self.logo(task, lambda *_, **kw: self.fail("image still claimed")))
        self.assertEqual(self.events(), {})
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE logo_image_claims SET lease_until=0")
        self.assertTrue(self.logo(task, lambda url, **_: (url, logo_png(), "image/png")))
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))

    def test_official_job_image_requirements_replace_evidence_and_link_only_body_preserves_it(self):
        self.seed([posting(1)])
        url = "https://job-boards.greenhouse.io/example/jobs/123"
        self.update_document(1, source="greenhouse", source_job_id=123, url=url)
        response = {
            "id": 123,
            "company_name": "회사",
            "title": "엔지니어",
            "absolute_url": url,
            "content": '<img alt="필수 경력 5년" src="https://org.example/requirements.png">',
            "updated_at": "2026-11-22T11:00:00Z",
        }

        def fetch(url, **_):
            return url, json.dumps(response).encode(), "application/json"

        self.deadlines([self.task(1)], fetch)
        stored = collect_jobs.get_posts(self.jobs, [1])[0]
        self.assertEqual(stored["lines"], ["필수 경력 5년"])
        self.assertEqual(
            stored["images"], [{"alt": "필수 경력 5년", "url": "https://org.example/requirements.png"}]
        )
        response.update(
            content='<a href="https://org.example/apply">Apply now</a>', updated_at="2026-11-23T11:00:00Z"
        )
        self.deadlines([self.task(1, id=2)], fetch)
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["images"], stored["images"])

    def test_greenhouse_response_body_logo_does_not_require_another_job_page(self):
        self.seed([posting(1)])
        url = "https://job-boards.greenhouse.io/example/jobs/123"
        asset = "https://org.example/logo.png"
        self.update_document(1, source="greenhouse", source_job_id=123, url=url)
        response = {
            "id": 123,
            "company_name": "회사",
            "title": "엔지니어",
            "absolute_url": url,
            "content": f'<p>요건</p><img alt="회사 로고" src="{asset}">',
        }
        calls = []

        def fetch(url, **_):
            calls.append(url)
            if url == asset:
                return url, logo_png(), "image/png"
            self.assertTrue(url.startswith("https://boards-api.greenhouse.io/"))
            return url, json.dumps(response).encode(), "application/json"

        task = self.task(1)
        self.deadlines([task], fetch)
        self.logo(dict(task, id=2, source_work_id=1, source_status="done"), fetch)
        self.assertEqual(self.events(), {1: "done", 2: "done"})
        self.assertEqual(len(calls), 2)

    def test_same_verified_company_without_a_second_asset_reuses_its_png(self):
        self.seed([posting(1), posting(2)])
        asset = "https://org.example/logo.png"
        for i in (1, 2):
            self.update_document(i, source="wanted", source_job_id=i, url=f"https://www.wanted.co.kr/wd/{i}")
        images = []

        def fetch(url, **_):
            if url == asset:
                images.append(url)
                return url, logo_png(), "image/png"
            i = int(url.rsplit("/", 1)[1])
            company = {"company_name": "회사", "company_id": 9}
            if i == 1:
                company["logo_image"] = asset
            page = next_page(
                {
                    "initialData": {
                        "id": i,
                        "position": "엔지니어",
                        "company": company,
                        "status": "active",
                        "requirements": "개발 경험",
                    }
                }
            )
            return url, page.encode(), "text/html"

        tasks = [self.task(i) for i in (1, 2)]
        self.deadlines(tasks, fetch)
        for task in tasks:
            self.logo(dict(task, id=task["id"] + 2, source_status="done", source_work_id=task["id"]), fetch)
        self.assertEqual(images, [asset])
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 2))

    def test_recollection_of_new_listing_metadata_preserves_same_official_evidence(self):
        url = "https://www.wanted.co.kr/wd/321"
        listing = posting(1, f'<p>지원 마감일: 2026-12-10</p><a href="{url}">지원하러가기</a>')
        self.seed([listing])

        def fetch(requested, **_):
            if requested == url:
                return (
                    url,
                    next_page(
                        {
                            "initialData": {
                                "id": 321,
                                "position": "엔지니어",
                                "company": {"company_name": "회사", "company_id": 1},
                                "status": "active",
                                "requirements": "검증된 Python 경력 3년",
                            }
                        }
                    ).encode(),
                    "text/html",
                )
            return requested, json.dumps([listing]).encode(), "application/json"

        self.deadlines([self.task(1)], fetch)
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision='exclude', reason='연차 확인', reviewed_at='2026-11-16'")
        updated = posting(
            1,
            f'<p>지원 마감일: 2026-12-20</p><img alt="회사 로고" src="https://org.example/logo.png"><a href="{url}/apply">지원하러가기</a>',
        )
        updated["modified_gmt"] = "2026-11-22T00:00:00"
        collect_jobs.collect(self.jobs, lambda _: ([updated], 1))
        stored = collect_jobs.get_posts(self.jobs, [1])[0]
        self.assertEqual(stored["lines"], ["검증된 Python 경력 3년"])
        self.assertEqual(stored["jd_source"]["original_id"], "321")
        card = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual((card["reason"], card["deadline_label"]), ("연차 확인", "12.20 마감"))
        updated["content"]["rendered"] = updated["content"]["rendered"].replace("/321/apply", "/999/apply")
        updated["modified_gmt"] = "2026-11-23T00:00:00"
        collect_jobs.collect(self.jobs, lambda _: ([updated], 1))
        self.assertNotIn("jd_source", collect_jobs.get_posts(self.jobs, [1])[0])

    def test_recollection_of_deadline_only_revision_preserves_inthiswork_jd_and_decision(self):
        self.seed([posting(1, "<p>필수 경력 5년</p>")])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision='exclude', reason='필수 경력 미충족'")
        fresh = posting(1, "<p>지원 마감일 2099-12-31</p>")
        fresh["modified_gmt"] = "2026-11-22T00:00:00"
        self.deadlines([self.task(1)], lambda url, **_: (url, json.dumps([fresh]).encode(), "application/json"))
        collect_jobs.collect(self.jobs, lambda _: ([fresh], 1))
        stored = collect_jobs.get_posts(self.jobs, [1])[0]
        self.assertEqual((stored["lines"], stored["decision"]), (["필수 경력 5년"], "exclude"))
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["deadline_label"], "12.31 마감")
        fresh["modified_gmt"] = "2026-11-23T00:00:00"
        fresh["title"]["rendered"] = "다른 회사｜엔지니어"
        collect_jobs.collect(self.jobs, lambda _: ([fresh], 1))
        self.assertEqual(collect_jobs.get_posts(self.jobs, [1])[0]["lines"], ["지원 마감일 2099-12-31"])

    def test_delayed_collection_cannot_revert_a_newer_deadline_only_manual_refresh(self):
        from job_search import fit_work
        from tests.test_job_fit import assessment, criterion

        image = "https://assets.example.com/original.png"
        self.seed([posting(1, f'<p>필수 경력 5년</p><img alt="회사 로고" src="{image}">')])
        fit_work.migrate(self.jobs, self.state, profile={})
        company_logos.refresh_logos(self.jobs, fetch=lambda u, **_: (u, logo_png(), "image/png"))
        before = self.task(1)
        entry = {"id": 1, "fingerprint": before["version"], "decision": "exclude", "reason": "필수 경력 미충족",
                 "evidence_lines": [1], "availability": "open", "assessment": assessment(criterion("경력", "required", "unmet"))}
        collect_jobs.record_reviews(self.jobs, [entry], execution_id="previous")
        old_fit = collect_jobs.get_posts(self.jobs, [1])[0]["fit_review"]
        original_logo = company_logos.read_logo(self.jobs, 1)
        self.assertIsNotNone(original_logo)
        newest = posting(1, "<p>지원 마감: 2099-12-31</p>")
        newest["modified_gmt"] = "2026-11-22T00:00:00"
        self.deadlines([before], lambda u, **_: (u, json.dumps([newest]).encode(), "application/json"))
        delayed = posting(1, "<p>이전 공고의 요건</p><p>지원 마감: 2000-01-01</p>")
        delayed["modified_gmt"] = "2026-11-20T00:00:00"
        result = collect_jobs.collect(self.jobs, lambda _: ([delayed], 1))
        self.assertEqual(result["changed"], 0)
        doc = collect_jobs.get_posts(self.jobs, [1])[0]
        self.assertEqual(doc["modified_gmt"], newest["modified_gmt"])
        self.assertEqual(doc["lines"], ["필수 경력 5년"])
        self.assertEqual(doc["fit_review"], {**old_fit, "fingerprint": doc["fingerprint"]})
        self.assertNotEqual(doc["fingerprint"], before["version"])
        self.assertEqual(company_logos.read_logo(self.jobs, 1), original_logo)
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["deadline_label"], "12.31 마감")

    def test_recollection_with_known_new_official_role_drops_old_listing_jd(self):
        first = posting(1, '<p>필수 경력 5년</p><a href="https://www.wanted.co.kr/wd/17">지원하러가기</a>')
        second = posting(1, '<p>마감일 2099-12-31</p><a href="https://www.wanted.co.kr/wd/18">지원하러가기</a>')
        second["modified_gmt"] = "2026-11-22T00:00:00"
        self.seed([first])
        collect_jobs.collect(self.jobs, lambda _: ([second], 1))
        self.assertNotIn("필수 경력 5년", collect_jobs.get_posts(self.jobs, [1])[0]["lines"])

    def test_later_sibling_logo_adds_a_completion_event_to_a_finished_role(self):
        from functools import partial

        asset = "https://assets.example.com/brand.png"
        self.seed([posting(1), posting(2)])
        for i in (1, 2):
            self.update_document(i, source="wanted", source_job_id=i, url=f"https://www.wanted.co.kr/wd/{i}")
        finish = partial(refresh_queue.finish, self.state)
        downloads = []

        def fetch(url, **_):
            if url == asset:
                downloads.append(url)
                return url, logo_png(), "image/png"
            i = int(url.rsplit("/", 1)[1])
            company = {"company_name": "회사", "company_id": 9}
            if i == 2:
                company["logo_image"] = asset
            page = next_page({"initialData": {"id": i, "position": "엔지니어", "company": company,
                                              "status": "active", "requirements": "개발 경험"}})
            return url, page.encode(), "text/html"

        past = refresh_queue.enqueue(self.state, [(1, self.task(1)["version"])], "detail:1")
        self.deadlines(refresh_queue.claim(self.state, "deadline", 1), fetch, finish)
        old_logo, = refresh_queue.claim(self.state, "logo", 1)
        self.logo(old_logo, fetch, finish)
        old_fit, = refresh_queue.claim(self.state, "fit", 1)
        finish(old_fit, "done")
        run = refresh_queue.enqueue(self.state, [(i, self.task(i)["version"]) for i in (1, 2)], "list")
        for i in (1, 2):
            self.deadlines(refresh_queue.claim(self.state, "deadline", 1), fetch, finish)
            task, = refresh_queue.claim(self.state, "logo", 1)
            self.assertTrue(self.logo(task, fetch, finish))
            if i == 1:
                first = refresh_queue.snapshot(self.state, run, 0)
                self.assertEqual(first["counts"]["skipped"], 1)
        after = refresh_queue.snapshot(self.state, run, first["cursor"])
        self.assertEqual(sorted((e["post_id"], e["status"]) for e in after["changes"] if e["kind"] == "logo"),
                         [(1, "done"), (2, "done")])
        self.assertEqual(downloads, [asset])
        self.assertEqual(company_logos.read_logo(self.jobs, 1), company_logos.read_logo(self.jobs, 2))
        self.assertEqual(refresh_queue.snapshot(self.state, past, 0)["counts"]["skipped"], 1)

    def test_verified_linked_closure_is_visible_and_survives_listing_only_periodic_check(self):
        from job_search.refresh_job_availability import refresh

        url = "https://www.wanted.co.kr/wd/17"
        listing = posting(1, f'<a href="{url}">지원하러가기</a>')
        self.seed([listing])
        post = {"id": 17, "position": "엔지니어", "company": {"company_id": 1, "company_name": "회사"},
                "status": "closed", "due_time": "2026-01-22", "requirements": "필수 경력 5년"}

        def fetch(requested, **_):
            if requested == url:
                return url, next_page({"initialData": post}).encode(), "text/html"
            return requested, json.dumps([listing]).encode(), "application/json"

        self.deadlines([self.task(1)], fetch)
        job = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertTrue(job["is_expired"])
        self.assertEqual(job["deadline_label"], "01.22 마감")
        self.assertEqual(job["lines"], ["필수 경력 5년"])
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision='recommend'")
        refresh(self.jobs, self.state, lambda _: ([listing], 1))
        latest = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertTrue(latest["is_expired"])
        self.assertEqual(latest["availability_checked_at"], job["availability_checked_at"])
        dashboard_store.set_deadline(self.jobs, self.state, 1, {"mode": "manual", "kind": "rolling", "word": "상시 채용"})
        post.update(status="active", due_time="2099-12-26")
        self.deadlines([self.task(1, id=2)], fetch)
        reopened = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(reopened["deadline_label"], "상시 채용")
        self.assertEqual(reopened["deadline_auto"]["label"], "12.26 마감")
        self.assertFalse(reopened["deadline_auto"]["closed"])
        post.update(due_time="bad-date", requirements="필수 경력 7년")
        self.deadlines([self.task(1, id=3)], fetch)
        partial = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(self.events()[3], "failed")
        self.assertEqual(partial["lines"], ["필수 경력 7년"])
        self.assertEqual(partial["availability_checked_at"], reopened["availability_checked_at"])
        self.assertEqual(partial["deadline_auto"]["label"], "12.26 마감")

    def test_linked_greenhouse_and_listing_revisions_have_independent_clocks(self):
        url = "https://job-boards.greenhouse.io/example/jobs/17"
        listing = posting(1, f'<a href="{url}">지원하러가기</a>')
        listing["modified_gmt"] = "2026-11-21T00:00:00"
        self.seed([listing])
        role = {"id": 17, "title": "엔지니어", "company_name": "회사", "absolute_url": url,
                "content": "<p>필수 경력 5년</p>", "application_deadline": "2099-11-15",
                "updated_at": "2026-11-10T00:00:00Z"}

        def fetch(requested, **_):
            response = [listing] if requested.startswith(collect_jobs.API) else role
            return requested, json.dumps(response).encode(), "application/json"

        self.deadlines([self.task(1)], fetch)
        first = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(first["deadline_label"], "11.15 마감")
        listing["modified_gmt"] = "2026-11-22T00:00:00"
        role.update(application_deadline="2099-11-30", updated_at="2026-11-09T00:00:00Z", content="")
        self.deadlines([self.task(1, id=2)], fetch)
        stale = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(stale["deadline_label"], first["deadline_label"])
        self.assertEqual(stale["availability_checked_at"], first["availability_checked_at"])
        role["updated_at"] = "2026-11-12T00:00:00Z"
        self.deadlines([self.task(1, id=3)], fetch)
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["deadline_label"], "11.30 마감")
        def outage(requested, **_):
            if not requested.startswith(collect_jobs.API):
                raise TimeoutError()
            return fetch(requested)

        before_outage = dashboard_store.get_job(self.jobs, self.state, 1)
        self.deadlines([self.task(1, id=4)], outage)
        after_outage = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(self.events()[4], "failed")
        self.assertEqual(after_outage["availability_checked_at"], before_outage["availability_checked_at"])
        self.assertEqual(after_outage["deadline_label"], "11.30 마감")

    def test_periodic_listing_check_works_before_linked_deadline_table_migration(self):
        from job_search.refresh_job_availability import refresh

        listing = posting(1, '<p>지원 마감일 2099-12-31</p><a href="https://www.wanted.co.kr/wd/17">지원하러가기</a>')
        self.seed([listing])
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("DROP TABLE job_linked_deadlines")
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.execute("UPDATE posts SET decision='recommend'")
        refresh(self.jobs, self.state, lambda _: ([listing], 1))
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 1)["deadline_label"], "12.31 마감")

    def test_malformed_stored_evidence_cannot_block_other_roles_in_the_same_include_response(self):
        records = [posting(1), posting(2, "<p>지원 마감일 2099-12-31</p>")]
        self.seed(records)
        self.update_document(1, lines=None)
        self.deadlines([self.task(1), self.task(2)], lambda url, **_: (url, json.dumps(records).encode(), "application/json"))
        self.assertEqual(self.events(), {1: "failed", 2: "done"})
        self.assertEqual(dashboard_store.get_job(self.jobs, self.state, 2)["deadline_label"], "12.31 마감")

    def test_listing_deadline_and_verified_logo_publish_before_linked_jd_returns(self):
        from functools import partial

        url = "https://www.wanted.co.kr/wd/17"
        image = "https://assets.example.com/17.png"
        old = posting(1, "<p>마감 2099-12-01</p>")
        self.seed([old])
        fresh = posting(
            1, f'<p>마감 2099-12-31</p><img alt="회사 로고" src="{image}"><a href="{url}">지원하러가기</a>'
        )
        fresh["modified_gmt"] = "2026-11-23T01:00:00"
        entered, release = threading.Event(), threading.Event()
        run = refresh_queue.enqueue(self.state, [(1, self.task(1)["version"])], "detail:1")
        finish = partial(refresh_queue.finish, self.state)

        def fetch(requested, **_):
            if requested == image:
                return image, logo_png(), "image/png"
            if requested == url:
                entered.set()
                self.assertTrue(release.wait(3))
                page = next_page(
                    {
                        "initialData": {
                            "id": 17,
                            "position": "엔지니어",
                            "company": {"company_id": 1, "company_name": "회사"},
                            "requirements": "공식 JD 3년",
                            "status": "active",
                        }
                    }
                )
                return url, page.encode(), "text/html"
            return requested, json.dumps([fresh]).encode(), "application/json"

        with ThreadPoolExecutor() as pool:
            tasks = refresh_queue.claim(self.state, "deadline", 1)
            future = pool.submit(self.deadlines, tasks, fetch, finish)
            try:
                self.assertTrue(entered.wait(2))
                card = dashboard_store.get_job(self.jobs, self.state, 1)
                self.assertEqual(card["deadline_label"], "12.31 마감")
                self.assertIsNotNone(card["availability_checked_at"])
                delayed = posting(1, "<p>이전 공고 필수 경력</p><p>마감 2000-01-01</p>")
                delayed["modified_gmt"] = "2026-11-22T23:00:00"
                self.assertEqual(collect_jobs.collect(self.jobs, lambda _: ([delayed], 1))["changed"], 0)
                partial_result = refresh_queue.snapshot(self.state, run, 0)
                self.assertEqual(
                    [(e["kind"], e["status"]) for e in partial_result["changes"]], [("deadline", "running")]
                )
                self.assertEqual(refresh_queue.claim(self.state, "fit", 1), [])
                (logo,) = refresh_queue.claim(self.state, "logo", 1)
                self.assertTrue(self.logo(logo, fetch, finish))
                self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))
            finally:
                release.set()
            future.result(3)
        current = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(current["lines"], ["공식 JD 3년"])
        self.assertEqual(current["availability_checked_at"], card["availability_checked_at"])
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))
        self.assertEqual(len(refresh_queue.claim(self.state, "fit", 1)), 1)
        final = refresh_queue.snapshot(self.state, run, partial_result["cursor"])
        self.assertEqual(
            [(e["kind"], e["status"]) for e in final["changes"]], [("logo", "done"), ("deadline", "done")]
        )

    def test_verified_jd_survives_invalid_deadline_while_last_success_stays_visible(self):
        self.seed([posting(1)])
        url = "https://www.wanted.co.kr/wd/321"
        self.update_document(1, source="wanted", source_job_id=321, url=url)
        data = {
            "id": 321,
            "company": {"company_id": 1, "company_name": "회사"},
            "position": "엔지니어",
            "status": "active",
            "due_time": "2099-12-15",
            "requirements": "공식 경력 2년",
        }

        def fetch(requested, **_):
            return url, next_page({"initialData": data}).encode(), "text/html"

        self.deadlines([self.task(1)], fetch)
        saved = dashboard_store.get_job(self.jobs, self.state, 1)
        data.update(due_time="invalid", requirements="공식 경력 5년")
        self.deadlines([self.task(1, id=2)], fetch)
        current = dashboard_store.get_job(self.jobs, self.state, 1)
        self.assertEqual(current["lines"], ["공식 경력 5년"])
        self.assertNotEqual(saved["fingerprint"], current["fingerprint"])
        self.assertEqual(current["availability_checked_at"], saved["availability_checked_at"])
        self.assertEqual(current["deadline_label"], "12.15 마감")
        self.assertEqual(self.events(), {1: "done", 2: "failed"})

    def test_same_name_employer_swap_cannot_replace_verified_job_or_scheduled_routing(self):
        from job_search.fit_work import verify_openings

        url = "https://www.wanted.co.kr/wd/17"
        listing = posting(2, f'<a href="{url}">지원하러가기</a>')
        self.seed([posting(1), listing])
        self.update_document(1, source="wanted", source_job_id=17, url=url)
        company = {"company_id": 1, "company_name": "회사"}
        role = {"id": 17, "position": "엔지니어", "company": company, "status": "active",
                "requirements": "기존 기업의 필수 요건", "due_time": "2099-12-15"}

        def fetch(requested, **_):
            if requested == url:
                return url, next_page({"initialData": role}).encode(), "text/html"
            if requested.startswith(collect_jobs.API):
                return requested, json.dumps([listing]).encode(), "application/json"
            return requested, logo_png(), "image/png"

        self.deadlines([self.task(1), self.task(2)], fetch)
        stored = collect_jobs.get_posts(self.jobs, [1, 2])
        company.update(company_id=2, logo_image="https://assets.example.com/other.png")
        role.update(status="closed", requirements="동명 기업의 필수 요건")
        scheduled = verify_openings(stored, fetch_page=lambda _: ([listing], 1), fetch=fetch)
        self.deadlines([self.task(1, id=3), self.task(2, id=4)], fetch)
        company_logos.refresh_logos(self.jobs, fetch=fetch)
        for post_id in (1, 2):
            card = dashboard_store.get_job(self.jobs, self.state, post_id)
            self.assertEqual(card["lines"], ["기존 기업의 필수 요건"])
            self.assertFalse(card["is_expired"])
            self.assertIsNone(company_logos.read_logo(self.jobs, post_id))
        self.assertEqual(self.events(), {1: "done", 2: "done", 3: "failed", 4: "failed"})
        self.assertEqual(scheduled, {})

    def test_invalid_early_image_waits_for_official_logo_without_refetching_the_page(self):
        from functools import partial

        url = "https://www.wanted.co.kr/wd/17"
        image, broken = "https://assets.example.com/good.png", "https://assets.example.com/broken.png"
        listing = posting(
            1, f'<p>마감 2099-12-01</p><img alt="회사 로고" src="{broken}"><a href="{url}">지원하러가기</a>'
        )
        self.seed([listing])
        refresh_queue.enqueue(self.state, [(1, self.task(1)["version"])], "detail:1")
        finish = partial(refresh_queue.finish, self.state)
        calls, entered, release = [], threading.Event(), threading.Event()

        def fetch(requested, **_):
            calls.append(requested)
            if requested == url:
                entered.set()
                self.assertTrue(release.wait(3))
                page = next_page(
                    {
                        "initialData": {
                            "id": 17,
                            "position": "엔지니어",
                            "company": {"company_id": 1, "company_name": "회사", "logo_image": image},
                            "requirements": "공식 JD",
                            "status": "active",
                        }
                    }
                )
                return url, page.encode(), "text/html"
            if requested in (broken, image):
                return requested, (b"invalid" if requested == broken else logo_png()), "image/png"
            return requested, json.dumps([listing]).encode(), "application/json"

        with ThreadPoolExecutor() as pool:
            tasks = refresh_queue.claim(self.state, "deadline", 1)
            future = pool.submit(self.deadlines, tasks, fetch, finish)
            try:
                self.assertTrue(entered.wait(2))
                (logo,) = refresh_queue.claim(self.state, "logo", 1)
                self.assertFalse(self.logo(logo, fetch, finish))
                self.assertFalse(self.logo(logo, fetch, finish))
            finally:
                release.set()
            future.result(3)
        self.assertTrue(self.logo(logo, fetch, finish))
        self.assertEqual(calls.count(url), 1)
        self.assertEqual(calls.count(broken), 1)
        self.assertEqual(calls.count(image), 1)
        self.assertIsNotNone(company_logos.read_logo(self.jobs, 1))
