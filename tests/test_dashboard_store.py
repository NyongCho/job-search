import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs
from job_search import dashboard_store as store
from job_search.refresh_job_availability import refresh
from tests.test_collect_jobs import posting


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "jobs.sqlite"
        self.state = Path(self.temp.name) / "private/state.sqlite"
        self.now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.collect([posting()])
        self.review(1)
        store.init_state_db(self.source, self.state)

    def collect(self, posts):
        collect_jobs.collect(self.source, lambda _: (posts, 1), self.now)

    def review(self, post_id, decision="recommend", stamp="2026-11-16T00:00:00+00:00"):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE posts SET decision=?,reason=?,evidence_lines='[1]',reviewed_at=? WHERE id=?",
                       (decision, "Agent 개발 경험과 연결되는 업무", stamp, post_id))

    def source_dump(self):
        with sqlite3.connect(self.source) as db:
            return "\n".join(db.iterdump())

    def test_source_read_only_and_missing_db(self):
        before = self.source_dump()
        store.list_jobs(self.source, self.state)
        store.set_status(self.source, self.state, 1, "interested")
        self.assertEqual(self.source_dump(), before)
        missing = self.source.with_name("missing.sqlite")
        with self.assertRaises(FileNotFoundError):
            store.init_state_db(missing, self.state)
        self.assertFalse(missing.exists())

    def test_rejects_same_database(self):
        symlink, hardlink = self.source.with_name("sym.sqlite"), self.source.with_name("hard.sqlite")
        symlink.symlink_to(self.source)
        hardlink.hardlink_to(self.source)
        before = self.source_dump()
        for candidate in [self.source, symlink, hardlink]:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                store.init_state_db(self.source, candidate)
        self.assertEqual(self.source_dump(), before)

    def test_state_survives_recollection(self):
        store.set_status(self.source, self.state, 1, "interested")
        self.collect([posting(content="<p>새로운 필수 요건</p>")])
        item = store.list_jobs(self.source, self.state, view="interested")["items"][0]
        self.assertIsNone(item["decision"])
        self.assertEqual(item["status"], "interested")
        self.review(1, "exclude")
        self.assertEqual(store.list_jobs(self.source, self.state, view="interested")["total"], 1)
        store.init_state_db(self.source, self.state)
        self.assertEqual(store.get_job(self.source, self.state, 1)["status"], "interested")

    def test_manual_deadline_and_automatic_refresh_do_not_change_application_state(self):
        closed = posting(categories=[collect_jobs.CLOSED])
        refresh(self.source, self.state, lambda _: ([closed], 1), self.now)
        store.set_status(self.source, self.state, 1, "interested")
        self.assertEqual(store.list_jobs(self.source, self.state)["counts"]["expired"], 1)
        store.set_deadline(self.source, self.state, 1, {"mode": "manual", "kind": "unknown"})
        self.assertTrue(store.get_job(self.source, self.state, 1)["is_expired"])
        store.set_deadline(self.source, self.state, 1, {"mode": "manual", "kind": "rolling", "word": "채용 시 마감"})
        self.assertEqual(store.list_jobs(self.source, self.state)["counts"]["expired"], 0)
        store.set_deadline(self.source, self.state, 1, {"mode": "manual", "kind": "datetime",
                                                         "date": "2099-12-01", "time": "18:00"})
        refresh(self.source, self.state, lambda _: ([closed], 1), self.now)
        job = store.get_job(self.source, self.state, 1)
        self.assertEqual(job["deadline_label"], "12.01 18:00 마감")
        self.assertEqual(job["status"], "interested")
        self.assertFalse(job["is_expired"])
        self.assertTrue(job["deadline_auto"]["is_expired"])
        store.set_deadline(self.source, self.state, 1, {"mode": "auto"})
        self.assertTrue(store.get_job(self.source, self.state, 1)["is_expired"])

    def test_restoring_automatic_requires_confirmation_when_new_expiry_is_unexpected(self):
        store.set_deadline(self.source, self.state, 1, {"mode": "manual", "kind": "rolling", "word": "상시 채용"})
        before = store.get_job(self.source, self.state, 1)["deadline_auto"]["version"]
        refresh(self.source, self.state, lambda _: ([posting(categories=[collect_jobs.CLOSED])], 1), self.now)
        with self.assertRaisesRegex(store.DeadlineChanged, "다시"):
            store.set_deadline(self.source, self.state, 1, {"mode": "auto", "version": before})
        latest = store.get_job(self.source, self.state, 1)
        self.assertEqual(latest["deadline_label"], "상시 채용")
        store.set_deadline(self.source, self.state, 1, {"mode": "auto", "version": latest["deadline_auto"]["version"]})
        self.assertTrue(store.get_job(self.source, self.state, 1)["is_expired"])

    def test_pending_review_has_no_unread_badge(self):
        stamp = store.get_job(self.source, self.state, 1)["reviewed_at"]
        self.assertTrue(store.mark_seen(self.source, self.state, 1, stamp))
        store.set_status(self.source, self.state, 1, "interested")
        self.collect([posting(content="<p>변경</p>")])
        job = store.get_job(self.source, self.state, 1)
        self.assertIsNone(job["reviewed_at"])
        self.assertFalse(job["is_unread"])
        self.assertFalse(store.mark_seen(self.source, self.state, 1, stamp))
        self.review(1, stamp="2026-11-16T01:00:00Z")
        self.assertTrue(store.get_job(self.source, self.state, 1)["is_unread"])

    def test_seen_revision_changed(self):
        before = store.get_job(self.source, self.state, 1)["reviewed_at"]
        self.review(1, stamp="2026-11-16T02:00:00Z")
        self.assertFalse(store.mark_seen(self.source, self.state, 1, before))
        self.assertTrue(store.get_job(self.source, self.state, 1)["is_unread"])

    def test_seen_never_regresses(self):
        store.set_status(self.source, self.state, 1, "interested")
        old = store.get_job(self.source, self.state, 1)["reviewed_at"]
        ready, release = threading.Event(), threading.Event()
        real_write = store._write_seen
        failures = []

        def delayed(*args):
            if threading.current_thread().name == "old-reader":
                ready.set()
                if not release.wait(5):
                    raise TimeoutError("test coordination")
            return real_write(*args)

        def worker():
            try:
                store.mark_seen(self.source, self.state, 1, old)
            except Exception as error:
                failures.append(error)

        with patch.object(store, "_write_seen", side_effect=delayed):
            thread = threading.Thread(target=worker, name="old-reader")
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.review(1, stamp="2026-11-16T03:00:00Z")
                latest = store.get_job(self.source, self.state, 1)["reviewed_at"]
                self.assertTrue(store.mark_seen(self.source, self.state, 1, latest))
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(failures)
        job = store.get_job(self.source, self.state, 1)
        self.assertFalse(job["is_unread"])
        self.assertEqual(job["status"], "interested")
        store.set_status(self.source, self.state, 1, "preparing")
        self.assertFalse(store.get_job(self.source, self.state, 1)["is_unread"])

    def test_saved_views_ignore_model_decision(self):
        for state in ["interested", "preparing", "applied", "archived"]:
            store.set_status(self.source, self.state, 1, state)
            self.review(1, "exclude")
            self.assertEqual(store.list_jobs(self.source, self.state, view=state)["total"], 1)
        self.review(1)
        self.assertEqual(store.list_jobs(self.source, self.state)["total"], 0)
        store.set_status(self.source, self.state, 1, "unmarked")
        self.assertEqual(store.list_jobs(self.source, self.state)["total"], 1)

    def test_partial_document(self):
        self.collect([posting(2), posting(3)])
        self.review(2)
        self.review(3)
        with sqlite3.connect(self.source) as db:
            doc = json.loads(db.execute("SELECT document FROM posts WHERE id=1").fetchone()[0])
            doc["apply_url"] = "javascript:alert(1)"
            doc["lines"] = ["지원하러 가기"]
            db.execute("UPDATE posts SET document=? WHERE id=1", (json.dumps(doc),))
            db.execute("UPDATE posts SET document='not json' WHERE id=2")
        self.assertIsNone(store.get_job(self.source, self.state, 1)["apply_url"])
        items = store.list_jobs(self.source, self.state)["items"]
        self.assertEqual(len(items), 3)
        self.assertTrue(next(x for x in items if x["id"] == 2)["display_error"])

    def test_search_pagination_and_lock(self):
        self.collect([posting(i) for i in range(2, 32)])
        for i in range(2, 32):
            self.review(i)
        second = store.list_jobs(self.source, self.state, page=2)
        self.assertEqual(len(second["items"]), 1)
        store.set_status(self.source, self.state, second["items"][0]["id"], "archived")
        self.assertEqual(store.list_jobs(self.source, self.state, page=2)["page"], 1)
        self.assertEqual(store.list_jobs(self.source, self.state, q="AGENT")["total"], 30)
        self.assertEqual(store.list_jobs(self.source, self.state, q="없는회사")["total"], 0)
        with sqlite3.connect(self.source) as locker:
            locker.execute("BEGIN EXCLUSIVE")
            start = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                store.list_jobs(self.source, self.state)
            self.assertLess(time.monotonic() - start, 3.5)

    def test_mixed_timestamp_formats(self):
        self.collect([posting(1000)])
        self.review(1000, stamp=None)
        store.set_status(self.source, self.state, 1000, "interested")
        store.set_status(self.source, self.state, 1, "interested")
        self.review(1, stamp="2026-11-14T00:00:00+00:00")
        self.assertEqual([j["id"] for j in store.list_jobs(self.source, self.state, view="interested")["items"]], [1000, 1])

    def test_validation_and_private_permissions(self):
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.parent.stat().st_mode & 0o777, 0o700)
        for kwargs in [{"view": "invalid"}, {"page": 0}, {"q": "x" * 201}]:
            with self.assertRaises(ValueError):
                store.list_jobs(self.source, self.state, **kwargs)
        with self.assertRaises(ValueError):
            store.set_status(self.source, self.state, 1, "hired")
        with self.assertRaises(LookupError):
            store.set_status(self.source, self.state, 999, "applied")
        self.assertIsNone(store.get_job(self.source, self.state, 999))


if __name__ == "__main__":
    unittest.main()
