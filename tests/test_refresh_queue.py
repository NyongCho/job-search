import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from job_search import collect_jobs, dashboard, dashboard_store as store, refresh_queue as queue
from tests.test_collect_jobs import posting


class RefreshQueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobs = Path(tmp.name) / "jobs.sqlite"
        self.state = Path(tmp.name) / "state.sqlite"
        collect_jobs.collect(self.jobs, lambda _: ([posting(i) for i in range(1, 35)], 1))
        with sqlite3.connect(self.jobs) as db:
            db.execute("UPDATE posts SET decision='recommend',reason=CASE WHEN id<34 THEN '대상' ELSE '기타' END")
        app = dashboard.create_app({"JOBS_DB": self.jobs, "STATE_DB": self.state, "BASE_URL": "http://localhost",
                                    "DEV_MODE": True, "SECRET_KEY": "x" * 40})
        self.web = app.test_client()
        with self.web.session_transaction() as session:
            session["csrf_token"] = "test"

    def start(self, data, query="?q=대상"):
        response = self.web.post("/refreshes" + query, json=data, headers={"X-CSRF-Token": "test"})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.json

    def test_all_pages_and_detail_join_visible_work_and_resume(self):
        run = self.start({}, "?q=대상&page=2")
        self.assertEqual(run["counts"]["queued"], 33 * 3)
        again = self.start({})
        self.assertEqual(again["id"], run["id"])
        task, = queue.claim(self.state, "deadline", 1)
        queue.finish(self.state, task, "done")
        detail = self.start({"post_id": task["post_id"]})
        self.assertEqual(detail["counts"]["done"], 1)
        self.assertEqual(detail["counts"]["queued"], 2)
        self.assertEqual(detail["by_kind"]["deadline"]["done"], 1)
        self.assertEqual(detail["by_kind"]["logo"]["queued"], 1)
        self.assertEqual(detail["by_kind"]["fit"]["queued"], 1)
        fresh = self.web.get(f"/refreshes/{run['id']}?after={run['cursor']}").json
        self.assertEqual(fresh["by_kind"]["deadline"]["queued"], 32)
        self.assertEqual([(c["kind"], c["post_id"]) for c in fresh["changes"]], [("deadline", task["post_id"])])
        self.assertIn(run["id"], [r["id"] for r in self.web.get("/refreshes?q=대상&page=2").json["runs"]])
        with sqlite3.connect(self.state) as db:
            self.assertFalse(db.execute("SELECT 1 FROM refresh_work WHERE post_id=34").fetchone())

    def test_list_enqueue_uses_a_bounded_number_of_sql_statements(self):
        from unittest.mock import patch
        original = queue.connect
        statements = []
        def trace(path):
            db = original(path)
            db.set_trace_callback(statements.append)
            return db
        with patch.object(queue, "connect", trace):
            run = queue.enqueue(self.state, [(i, "fixed") for i in range(100)], "list:all")
        self.assertEqual(queue.snapshot(self.state, run)["counts"]["queued"], 300)
        self.assertLess(len(statements), 25)

    def test_target_selection_does_not_build_card_links(self):
        from unittest.mock import patch
        with patch.object(store, "source_locator", side_effect=AssertionError("rendered a card for a queue target")):
            self.assertEqual(len(store.refresh_targets(self.jobs, self.state, q="대상")), 33)
            self.assertEqual(store.refresh_targets(self.jobs, self.state, post_id=1)[0][0], 1)

    def test_refresh_targets_match_list_membership_for_every_view(self):
        from tests.test_dashboard_store import collect_fit_examples
        collect_fit_examples(self.jobs)
        store.import_profile_jobs(self.jobs, self.state, [{"source_key": "applications/old", "company": "과거",
            "role": "Agent", "summary": "확인한 지원", "confirmed_on": "2026-11-15"}])
        for post_id, status in ((1, "applied"), (3, "preparing"), (4, "interested"), (6, "archived")):
            store.set_status(self.jobs, self.state, post_id, status)
        store.set_deadline(self.jobs, self.state, 2, {"mode": "manual", "kind": "date", "date": "2000-01-01"})
        for view in store.VIEWS:
            for q in ("", " AGENT "):
                with self.subTest(view=view, q=q):
                    listing = store.list_jobs(self.jobs, self.state, view=view, q=q)
                    expected = listing["items"]
                    for page in range(2, listing["total_pages"] + 1):
                        expected += store.list_jobs(self.jobs, self.state, view=view, q=q, page=page)["items"]
                    targets = store.refresh_targets(self.jobs, self.state, view=view, q=q)
                    self.assertCountEqual(targets, [(item["id"], item["fingerprint"]) for item in expected])

    def test_source_update_joins_its_own_inflight_logo_on_next_click(self):
        first = queue.enqueue(self.state, [(1, "old")], "detail:1")
        task, = queue.claim(self.state, "deadline", 1)
        queue.finish(self.state, {**task, "applied_version":"new"}, "done")
        second = queue.enqueue(self.state, [(1, "new")], "detail:1")
        self.assertEqual(queue.snapshot(self.state, second)["counts"]["done"], 1)
        self.assertEqual(queue.claim(self.state,"deadline",1), [])
        self.assertEqual(queue.claim(self.state,"logo",1)[0]["version"], "old")

    def test_racing_starts_share_work_and_restart_does_not_lose_changes(self):
        targets = store.refresh_targets(self.jobs, self.state, post_id=1)
        def enqueue(_):
            return queue.enqueue(self.state, targets, "detail:1")
        with ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(enqueue, range(3)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(queue.claim(self.state, "fit", 2), [])
        task, = queue.claim(self.state, "deadline", 1)
        queue.reset_running(self.state)
        reclaimed, = queue.claim(self.state, "deadline", 1)
        self.assertEqual(task["id"], reclaimed["id"])
        with sqlite3.connect(self.state) as db:
            queue.finish(self.state, reclaimed, "failed", "확인 실패", db=db)
        fit, = queue.claim(self.state, "fit", 1)
        self.assertEqual(fit["source_work_id"], task["id"])
        queue.finish(self.state, fit, "done")
        logo, = queue.claim(self.state, "logo", 1)
        queue.finish(self.state, logo, "skipped")
        final = queue.snapshot(self.state, ids[0])
        self.assertTrue(final["complete"])
        self.assertEqual(final["counts"]["failed"], 1)
        self.assertNotEqual(enqueue(None), ids[0])

    def test_client_receipt_recovers_lost_response_even_after_scope_finishes(self):
        url = "/refreshes?q=대상"
        key = "645572ec-ecdd-480f-9029-8e1152f06a4e"
        headers = {"X-CSRF-Token":"test", "X-Refresh-Request":key}
        run = self.web.post(url, json={"post_id":1}, headers=headers).json
        for kind in ("deadline", "logo", "fit"):
            task, = queue.claim(self.state, kind, 1)
            queue.finish(self.state, task, "done")
        self.assertEqual(self.web.get("/refreshes?request="+key).json["runs"][0]["id"], run["id"])
        self.assertEqual(self.web.post(url, json={"post_id":1}, headers=headers).json["id"], run["id"])
        self.assertEqual(self.web.post(url, json={"post_id":2}, headers=headers).status_code,400)

    def test_job_without_source_jd_shows_persisted_hold_in_detail_and_applied_page(self):
        from job_search import fit_work
        store.import_profile_jobs(self.jobs, self.state, [{"source_key":"applications/a",
            "company":"A", "role":"R", "summary":"서류 합격", "confirmed_on":"2026-11-11"}])
        run = self.start({"post_id":-1})
        deadline, = queue.claim(self.state,"deadline",1)
        queue.finish(self.state,deadline,"skipped")
        tasks = queue.claim(self.state,"fit",1)
        fit_work.refresh_fit_tasks(self.jobs,self.state,tasks,
                                   lambda *args: queue.finish(self.state,*args),
                                   profile={},runner=lambda *_: self.fail("JD가 없는 기록에서 모델 호출"))
        selected = store.get_job(self.jobs,self.state,-1)
        self.assertIsNone(selected["fit"]["score"])
        self.assertIn("원문",selected["fit"]["hold_reason"])
        self.assertEqual(selected["fit"],store.list_jobs(self.jobs,self.state,view="applied")["items"][0]["fit"])
        self.assertEqual(self.web.get(f"/refreshes/{run['id']}").json["usage"], [])

    def test_usage_links_report_owned_batch_once_joined_zero_and_missing_counters(self):
        from job_search import fit_work

        run = self.start({})
        joined = self.start({"post_id": 1})
        html = self.web.get("/?q=대상", headers={"X-Dashboard-Snapshot": "1"}).get_data(as_text=True)
        url = f"/refreshes/{run['id']}?view=usage"
        self.assertIn(f'href="{url}"', html)
        self.assertIn('모델 사용량', html)
        self.assertNotIn("usage", self.web.get(f"/refreshes/{run['id']}?view=progress").json)
        self.assertIn("<dd>0회</dd>", self.web.get(url).get_data(as_text=True))
        fit_work.migrate(self.jobs, self.state, profile={})
        with sqlite3.connect(self.jobs) as db:
            db.execute("DROP TABLE fit_attempt_origins")  # This attempt predates request attribution.
            db.execute("""INSERT INTO fit_attempts VALUES('batch',0,1.2,2,2,0,
                '{"calls":2,"input_tokens":1234,"output_tokens":null}')""")
            work_ids = queue.fit_work_ids(self.state, run["id"])[:2]
            db.executemany("INSERT INTO fit_watches VALUES(?,'batch',1)", [(str(i),) for i in work_ids])
            db.execute("""INSERT INTO fit_attempts VALUES('scheduled',0,1.2,1,1,0,'{"calls":1}')""")
            db.executemany("INSERT INTO fit_watches VALUES(?,'scheduled',?)",
                           [("cron:scheduled:1", 1), (str(work_ids[0]), 0)])
        owned = self.web.get(url).get_data(as_text=True)
        self.assertIn("<dd>2회</dd>", owned)
        self.assertIn("<dd>1,234</dd>", owned)
        self.assertIn("<dd>미측정</dd>", owned)
        shared = self.web.get(f"/refreshes/{joined['id']}?view=usage").get_data(as_text=True)
        self.assertIn("<dd>0회</dd>", shared)
        self.assertIn("다른 요청에서 시작한 평가 2회", shared)

    def test_finished_detail_and_owning_list_keep_their_usage_links_after_navigation(self):
        import re

        listing = self.start({})["id"]
        detail = self.start({"post_id": 1})["id"]
        snapshot_headers = {"X-Dashboard-Snapshot": "1"}

        def usage_links(markup):
            return [int(run) for run in re.findall(r'href="/refreshes/(\d+)\?view=usage"', markup)]

        for kind in queue.KINDS:
            tasks = queue.claim(self.state, kind, 1)
            self.assertEqual([task["post_id"] for task in tasks], [1])
            queue.finish(self.state, tasks[0], "failed" if kind == "logo" else "done")
        self.assertTrue(queue.snapshot(self.state, detail)["complete"])
        self.assertFalse(queue.snapshot(self.state, listing)["complete"])
        page = self.web.get("/jobs/1?q=대상", headers=snapshot_headers).get_data(as_text=True)
        list_pane, detail_pane = page.split('<article id="job-detail"', 1)
        self.assertIn(listing, usage_links(list_pane))
        self.assertIn(detail, usage_links(detail_pane))

        for kind in queue.KINDS:
            for task in queue.claim(self.state, kind, 50):
                queue.finish(self.state, task, "done")
        self.assertTrue(queue.snapshot(self.state, listing)["complete"])
        page = self.web.get("/?q=대상&page=2", headers=snapshot_headers).get_data(as_text=True)
        self.assertEqual(usage_links(page), [listing])
        page = self.web.get("/jobs/1?q=대상", headers=snapshot_headers).get_data(as_text=True)
        list_pane, detail_pane = page.split('<article id="job-detail"', 1)
        self.assertEqual(usage_links(list_pane), [listing])
        self.assertEqual(usage_links(detail_pane), [detail])

    def test_production_startup_requires_explicit_refresh_migration(self):
        from unittest.mock import patch
        from job_search import refresh_worker
        env = {**dashboard._environment(), "JOBS_DB":self.jobs, "STATE_DB":self.state, "BASE_URL":"https://jobs.example",
               "DEV_MODE":False, "SECRET_KEY":"x"*40, "ALLOWED_LOGIN":"user"}
        with patch.object(dashboard, "_environment", return_value=env):
            with self.assertRaisesRegex(RuntimeError, "refresh_worker"):
                dashboard.create_production_app()
            refresh_worker.migrate(self.jobs,self.state,schema_only=True)
            app = dashboard.create_production_app()
            self.assertEqual(app.test_client().get("/healthz",base_url=env["BASE_URL"]).status_code,200)

    def test_install_can_prepare_an_empty_state_database_after_collector_init(self):
        from job_search.refresh_worker import migrate
        state = self.state.with_name("new") / "dashboard.sqlite"
        migrate(self.jobs, state, schema_only=True)
        with sqlite3.connect(state) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM refresh_groups").fetchone()[0],0)

    def test_new_click_while_logo_is_pending_rechecks_current_profile_on_same_fit_task(self):
        run = self.start({"post_id":1})
        deadline, = queue.claim(self.state,"deadline",1)
        queue.finish(self.state,deadline,"done")
        first, = queue.claim(self.state,"fit",1)
        queue.finish(self.state,first,"done")
        cursor = queue.snapshot(self.state,run["id"])["cursor"]
        self.start({"post_id":1})
        second, = queue.claim(self.state,"fit",1)
        self.assertEqual(second["id"],first["id"])
        self.start({"post_id":1})  # an answer can change during the evaluation
        queue.finish(self.state,second,"done")
        third, = queue.claim(self.state,"fit",1)
        queue.finish(self.state,third,"done")
        updates = queue.snapshot(self.state,run["id"],cursor)
        self.assertGreater(updates["cursor"],cursor)
        self.assertEqual(updates["changes"][-1]["kind"],"fit")
        self.assertEqual(queue.claim(self.state,"deadline",1),[])

    def test_profile_recheck_charges_the_request_that_requested_the_new_evaluation(self):
        from functools import partial
        from job_search import fit_work
        from tests.test_fit_work import response

        store.set_status(self.jobs, self.state, 1, "applied")
        listing = self.start({}, "?view=applied")["id"]
        deadline, = queue.claim(self.state, "deadline", 1)
        queue.finish(self.state, deadline, "done")
        calls = []

        def evaluate(task, profile):
            def runner(attempt, evidence, docs):
                calls.append(attempt)
                return response(attempt, evidence, docs)
            fit_work.refresh_fit_tasks(self.jobs, self.state, [task], partial(queue.finish, self.state),
                                       profile={"P1": profile}, runner=runner)

        first, = queue.claim(self.state, "fit", 1)
        evaluate(first, "기존 경력")
        detail = self.start({"post_id": 1})["id"]  # The list's first fit finished, its logo is pending.
        second, = queue.claim(self.state, "fit", 1)
        self.start({}, "?view=applied")  # While the detail check is running, the list requests the next check.
        evaluate(second, "변경된 경력")
        interrupted, = queue.claim(self.state, "fit", 1)
        queue.reset_running(self.state)
        third, = queue.claim(self.state, "fit", 1)
        self.assertEqual(third, interrupted)
        evaluate(third, "한 번 더 변경된 경력")

        self.assertEqual(len(calls), 3)
        for run, owners in [(listing, [True, False, True]), (detail, [False, True, False])]:
            attempts = self.web.get(f"/refreshes/{run}").json["usage"]
            self.assertEqual([a["attempt_id"] for a in attempts], calls)
            self.assertEqual([a["owned"] for a in attempts], owners)

    def test_migration_keeps_queued_work_and_original_request_ownership(self):
        from job_search import refresh_worker

        run = self.start({"post_id": 1})["id"]
        with sqlite3.connect(self.state) as db:
            db.execute("ALTER TABLE refresh_work DROP COLUMN fit_run_id")
        refresh_worker.migrate(self.jobs, self.state, schema_only=True)
        self.assertEqual(queue.snapshot(self.state, run)["counts"]["queued"], 3)
        deadline, = queue.claim(self.state, "deadline", 1)
        queue.finish(self.state, deadline, "done")
        fit, = queue.claim(self.state, "fit", 1)
        self.assertEqual(fit["run_id"], run)

    def test_empty_scope_and_invalid_requests(self):
        self.assertTrue(self.start({}, "?q=없는공고")["complete"])
        for data, expected in [({"post_id": True}, 400), ({"post_id": 90}, 404), ({"ids": [1]}, 400)]:
            res = self.web.post("/refreshes", json=data, headers={"X-CSRF-Token": "test"})
            self.assertEqual(res.status_code, expected)
        self.assertEqual(self.web.post("/refreshes", json={}).status_code, 403)


class WorkerTests(unittest.TestCase):
    def test_worker_replays_domain_receipt_after_busy_queue_publication(self):
        import threading
        from contextlib import closing
        from job_search import refresh_worker
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with closing(sqlite3.connect(path)) as db:
                queue.create_tables(db)
            run = queue.enqueue(path, [(1, "jd")], "detail:1")
            stop = threading.Event()
            published = threading.Event()
            external_calls = []
            receipts = {}

            def logo(jobs, state, task, finish):
                if task["id"] not in receipts:
                    external_calls.append(task["id"])
                    receipts[task["id"]] = "done"
                    with closing(sqlite3.connect(state)) as lock, closing(sqlite3.connect(state, timeout=0)) as writer:
                        lock.execute("BEGIN IMMEDIATE")
                        writer.execute("BEGIN IMMEDIATE")
                finish(task, receipts[task["id"]])
                published.set()
                return True

            def skip(jobs, state, tasks, finish):
                for task in tasks:
                    finish(task, "skipped")

            worker = threading.Thread(target=refresh_worker.serve, args=(path, path, stop),
                                      kwargs=dict(deadlines=skip, logos=logo, fits=skip, poll=0.01))
            worker.start()
            try:
                self.assertTrue(published.wait(5))
            finally:
                stop.set()
                worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(external_calls), 1)
            self.assertEqual(queue.snapshot(path, run)["counts"]["done"], 1)

    def test_worker_runs_components_without_waiting_for_slow_logo(self):
        import threading
        from job_search import refresh_worker
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with sqlite3.connect(path) as db:
                queue.create_tables(db)
            run = queue.enqueue(path, [(1, "v1")], "detail:1")
            stop = threading.Event()
            logo_entered = threading.Event()
            release_logo = threading.Event()
            saw_fit = threading.Event()
            def deadlines(jobs, state, tasks, finish):
                self.assertTrue(logo_entered.wait(3))
                for task in tasks:
                    finish(task, "done")
            def logo(jobs, state, task, finish):
                logo_entered.set()
                release_logo.wait(4)
                finish(task, "done")
                return True
            def fit(jobs, state, tasks, finish):
                for task in tasks:
                    finish(task, "done")
                saw_fit.set()
            def worker():
                refresh_worker.serve(path, path, stop, deadlines=deadlines, logos=logo, fits=fit, poll=0.01)
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(saw_fit.wait(4))
                self.assertEqual(queue.snapshot(path, run)["counts"]["done"], 2)
            finally:
                stop.set()
                release_logo.set()
                thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertTrue(queue.snapshot(path, run)["complete"])
