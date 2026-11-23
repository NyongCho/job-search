"""Offline dashboard acceptance test for a historical recommendation inbox."""

import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from job_search import collect_jobs, dashboard, fit_work, refresh_sources, refresh_worker
from tests.test_collect_jobs import posting
from tests.test_fit_work import response


class RefreshIntegrationTests(unittest.TestCase):
    def test_two_page_refresh_submits_only_three_missing_evaluations_and_never_redelivers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            jobs, state = path / "jobs.sqlite", path / "state.sqlite"
            profile = {"P1": "공공 데이터 운영 경력"}
            unassessed = {1, 17, 33}
            records = {}
            for i in range(1, 35):
                records[i] = posting(
                    i,
                    f"<p>공공 데이터 운영 {i}</p><p>지원 마감: 2099-12-31</p>"
                    f'<a href="https://forms.gle/example{i}">지원하러가기</a>',
                )
                company = "공공기업" if i != 34 else "다른기업"
                records[i]["title"]["rendered"] = f"{company} {i:02d}｜개발자"
            collect_jobs.collect(jobs, lambda _: (list(records.values()), 1))
            historical = [
                dict(
                    id=doc["id"],
                    fingerprint=doc["fingerprint"],
                    decision="recommend",
                    reason="과거 추천",
                    evidence_lines=[1],
                )
                for doc in collect_jobs.get_posts(jobs, list(records))
            ]
            collect_jobs.record_reviews(jobs, historical)

            app = dashboard.create_app(
                {
                    "TESTING": True,
                    "DEV_MODE": True,
                    "BASE_URL": "http://localhost",
                    "JOBS_DB": jobs,
                    "STATE_DB": state,
                    "COMPANY_RESEARCH": path / "companies.json",
                    "SECRET_KEY": "local-fixture-" * 4,
                }
            )
            web = app.test_client()
            fit_work.migrate(jobs, state, profile=profile)

            def previous_evaluations(attempt, facts, docs):
                result = response(attempt, facts, docs)
                for entry in result["entries"]:
                    check = entry["assessment"]["criteria"][0]
                    if entry["id"] == 2:
                        check["status"] = "unmet"
                    if entry["id"] == 3:
                        check.update(status="unknown", profile_ids=[])
                        entry["assessment"]["question"] = "공공 데이터 운영 경험이 있나요?"
                return result

            def expect_saved(task, status, message):
                self.assertEqual(status, "done", (task, message))

            fit_work.refresh_fit_tasks(
                jobs,
                state,
                [{"id": f"history-{i}", "post_id": i} for i in range(1, 34) if i not in unassessed],
                expect_saved,
                runner=previous_evaluations,
                profile=profile,
                batch_size=5,
                concurrency=1,
            )
            saved = collect_jobs.get_posts(jobs, list(records))
            self.assertEqual({doc["id"] for doc in saved if not doc.get("fit_review")}, unassessed | {34})
            self.assertEqual(saved[1]["fit_review"]["score"], 0)
            self.assertIsNone(saved[2]["fit_review"]["score"])

            query = {"view": "recommended", "q": "공공기업", "sort": "company", "page": "2"}
            pages = []
            for page in (1, 2):
                listing = web.get("/", query_string={**query, "page": page})
                self.assertEqual(listing.status_code, 200)
                pages.append(
                    {
                        int(i)
                        for i in re.findall(
                            r'class="job-row [^"]*"[^>]+data-job-id="(\d+)"', listing.get_data(as_text=True)
                        )
                    }
                )
            self.assertEqual([len(ids) for ids in pages], [30, 3])
            self.assertEqual(pages[0] | pages[1], set(range(1, 34)))
            self.assertTrue(all(unassessed & ids for ids in pages))
            with web.session_transaction() as session:
                csrf = session["csrf_token"]

            submitted = []

            def evaluate(attempt, facts, docs):
                submitted.extend(doc["id"] for doc in docs)
                return response(attempt, facts, docs)

            def source(url, **_):
                self.assertEqual(urlsplit(url).path, "/wp-json/wp/v2/posts")
                ids = [int(i) for i in parse_qs(urlsplit(url).query)["include"][0].split(",")]
                return url, json.dumps([records[i] for i in ids]).encode(), "application/json"

            def click():
                accepted = web.post("/refreshes", json={}, query_string=query, headers={"X-CSRF-Token": csrf})
                self.assertEqual(accepted.status_code, 202)
                run = accepted.get_json()["id"]
                deadline = time.monotonic() + 10
                while True:
                    result = web.get(f"/refreshes/{run}?after=0").get_json()
                    if result["complete"]:
                        break
                    self.assertLess(time.monotonic(), deadline, result["counts"])
                    time.sleep(0.02)
                fit = {e["post_id"]: e["status"] for e in result["changes"] if e["kind"] == "fit"}
                self.assertEqual(fit, dict.fromkeys(range(1, 34), "done"))
                self.assertEqual(result["counts"]["failed"], 0)
                return result

            stop = threading.Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(
                    refresh_worker.serve,
                    jobs,
                    state,
                    stop,
                    deadlines=partial(refresh_sources.refresh_deadlines, fetch=source),
                    logos=partial(refresh_sources.refresh_logo_task, fetch=source),
                    fits=partial(
                        fit_work.refresh_fit_tasks, profile=profile, runner=evaluate, batch_size=5, concurrency=1
                    ),
                    poll=0.01,
                    fit_batch=5,
                )
                try:
                    first = click()
                    self.assertCountEqual(submitted, unassessed)
                    self.assertEqual(sum(row["submitted"] for row in first["usage"]), 3)
                    seen = list(submitted)
                    second = click()
                    self.assertNotEqual(second["id"], first["id"])
                    self.assertEqual(submitted, seen)
                    self.assertEqual(second["usage"], [])
                finally:
                    stop.set()
                    worker.result(timeout=5)

            forbidden = lambda *args: self.fail("historical recommendations were scheduled for delivery")
            digest = fit_work.scheduled_review(
                jobs, state, "next-scheduled-run", profile=profile, runner=forbidden, verifier=forbidden
            )
            self.assertEqual(digest, "[SILENT]")
            with closing(sqlite3.connect(jobs)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
            self.assertEqual(collect_jobs.reconcile_outbox(jobs, {}, "delivery-recovery"), [])
