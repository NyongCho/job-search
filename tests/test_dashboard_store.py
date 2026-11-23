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


FIT_EXAMPLES = (
    (2, "라온", "preferred", 9, 12),
    (3, "가온", "outside", 9, 10),
    (4, "다온", "outside", 799, 1000),
    (5, "가람", "preferred", None, None),
    (6, "나래", "preferred", 7, 8),
)


def collect_fit_examples(source, examples=FIT_EXAMPLES):
    """Add scored reviews alongside the existing unscored recommendation (#1)."""
    posts = []
    entries = []
    for post_id, company, preference, months, required in examples:
        requirement = "장애 대응 경험" if months is None else f"운영 경력 {required}개월"
        post = posting(post_id, content=f'<p>{requirement}</p><a href="https://example.com/job">지원하기</a>')
        post["title"]["rendered"] = f"{company}｜엔지니어"
        posts.append(post)
        criterion = {"key": "operations", "category": "required", "label": requirement, "evidence_lines": [1]}
        if months is None:
            criterion.update(status="unknown", evidence="")
        else:
            criterion.update(status="tenure", months=months, required_months=required,
                             evidence=f"서비스 배포와 모니터링을 {months}개월 담당")
        entries.append({
            "id": post_id, "decision": "needs_review" if months is None else "recommend",
            "reason": "Agent 서비스 운영 업무", "evidence_lines": [1], "apply_url": "https://example.com/job",
            "deadline": "미기재", "availability": "open", "required_summary": requirement,
            "assessment": {"version": 1, "complete": True, "preference": preference, "eligibility": "eligible",
                           "criteria": [criterion], "question": "운영 장애에 직접 대응했나요?" if months is None else ""},
        })
    collect_jobs.collect(source, lambda _: (posts, 1), datetime(2026, 11, 16, tzinfo=timezone.utc))
    for entry in entries:
        entry["fingerprint"] = collect_jobs.get_posts(source, [entry["id"]])[0]["fingerprint"]
    collect_jobs.record_reviews(source, entries, execution_id="dashboard-examples")


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

    def test_verified_application_link_is_shared_by_digest_and_current_dashboard_review(self):
        collect_fit_examples(self.source)
        verified = "https://example.com/job"
        doc = collect_jobs.get_posts(self.source, [2])[0]
        self.assertIsNone(doc["apply_url"])  # The original anchor says only "지원하기".
        digest, = collect_jobs.reconcile_outbox(self.source, {}, "retry")
        self.assertIn("지원 " + verified, digest)
        self.assertEqual(doc["fit_review"].get("apply_url"), verified)
        listing = store.list_jobs(self.source, self.state)
        self.assertEqual(next(j for j in listing["items"] if j["id"] == 2)["apply_url"], verified)
        self.assertEqual(store.get_job(self.source, self.state, 2)["apply_url"], verified)

        replacement = "https://example.com/new-opening"
        self.collect([posting(2, content=f'<p>변경 공고</p><a href="{replacement}">지원하러 가기</a>')])
        current = store.get_job(self.source, self.state, 2)
        self.assertIsNone(current["fit"])
        self.assertNotEqual(current["fingerprint"], doc["fingerprint"])
        self.assertEqual(current["apply_url"], replacement)

    def test_fit_groups_and_questions_are_filtered_before_pagination(self):
        collect_fit_examples(self.source)
        with patch.object(store, "PAGE_SIZE", 2):
            pages = [store.list_jobs(self.source, self.state, q="엔지니어", page=p) for p in (1, 2, 3)]
        self.assertEqual([[job["id"] for job in page["items"]] for page in pages], [[6, 2], [3, 1], [5]])
        for page in pages:
            self.assertEqual((page["counts"]["recommended"], page["total"], page["question_total"]), (4, 4, 1))
        self.assertEqual([job["id"] for job in store.list_jobs(self.source, self.state, sort="company")["items"]],
                         [3, 6, 2, 1, 5])
        only_question = store.list_jobs(self.source, self.state, q="가람")
        self.assertEqual((only_question["total"], only_question["question_total"]), (0, 1))
        self.assertEqual([job["id"] for job in store.list_jobs(self.source, self.state, view="needs_review")["items"]], [5])
        for status in ("applied", "archived"):
            store.set_status(self.source, self.state, 5, status)
            self.assertEqual(store.list_jobs(self.source, self.state)["question_total"], 0)
        store.set_status(self.source, self.state, 5, "unmarked")
        store.set_deadline(self.source, self.state, 5, {"mode": "manual", "kind": "date", "date": "2000-01-01"})
        self.assertEqual(store.list_jobs(self.source, self.state)["question_total"], 0)

    def test_inbox_fit_sort_ranks_all_search_matches_before_pagination(self):
        collect_fit_examples(self.source, (
            (2, "가온", "outside", 92, 100),
            (3, "나래", "preferred", 78, 100),
            (4, "다온", "preferred", 78, 100),
            (5, "라온", "outside", 0, 100),
            (6, "마루", "preferred", None, None),
            (7, "바다", "preferred", 100, 100),
        ))
        with sqlite3.connect(self.source) as db:
            for post_id, day in ((3, "2026-11-17"), (4, "2026-11-18"), (5, "2026-11-14"), (6, "2026-11-19")):
                db.execute("""UPDATE posts SET reviewed_at=?, fit_review=json_set(fit_review, '$.reviewed_at', ?)
                              WHERE id=?""", (day, day, post_id))
            db.execute("UPDATE posts SET reason='다른 업무' WHERE id=7")
        for post_id in range(1, 8):
            store.set_status(self.source, self.state, post_id, "interested")
        with patch.object(store, "PAGE_SIZE", 2):
            for page, expected in enumerate(([2, 4], [3, 5], [6, 1]), start=1):
                listing = store.list_jobs(self.source, self.state, view="interested", q="Agent", sort="fit", page=page)
                self.assertEqual([job["id"] for job in listing["items"]], expected)
                self.assertEqual(listing["total"], 6)

    def test_applied_fit_sort_places_imported_records_after_scored_jobs(self):
        collect_fit_examples(self.source)
        store.set_status(self.source, self.state, 4, "applied")
        store.import_profile_jobs(self.source, self.state, [{
            "source_key": "applications/legacy", "company": "기존 기업", "role": "개발자",
            "summary": "지원 기록", "confirmed_on": "2026-11-15",
        }])
        listing = store.list_jobs(self.source, self.state, view="applied", sort="fit")
        self.assertEqual([job["id"] for job in listing["items"]], [4, -1])

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

    def test_reviewed_sort_uses_actual_instant_and_puts_missing_reviews_last(self):
        self.collect([posting(1000)])
        self.review(1000, stamp=None)
        store.set_status(self.source, self.state, 1000, "interested")
        store.set_status(self.source, self.state, 1, "interested")
        self.review(1, stamp="2026-11-14T00:00:00+00:00")
        def interested_ids():
            return [j["id"] for j in store.list_jobs(self.source, self.state, view="interested")["items"]]

        self.assertEqual(interested_ids(), [1, 1000])
        self.review(1000, stamp="2026-11-14T08:59:00+09:00")
        self.assertEqual(interested_ids(), [1, 1000])
        self.review(1000, stamp="2026-11-14T09:00:00+09:00")
        self.assertEqual(interested_ids(), [1000, 1])

    def test_deadlines_use_effective_time_and_most_recent_known_expiry(self):
        contents = ["지원 마감: 2099-12-02", "지원 마감: 2099-12-02 18:00", "상시 채용",
                    "기한 미정", "지원 마감: 2020-11-20", "지원 마감: 2020-11-10",
                    "접수 종료", "접수 종료</p><p>지원 마감: 2099-12-01"]
        self.collect([posting(i, content=f"<p>{line}</p>") for i, line in enumerate(contents, 1)])
        for i in range(1, 9):
            store.set_status(self.source, self.state, i, "interested")

        def ids(view):
            return [j["id"] for j in store.list_jobs(self.source, self.state, view=view, sort="deadline")["items"]]

        self.assertEqual(ids("interested"), [2, 1, 3, 4, 5, 6, 8, 7])
        self.assertEqual(ids("expired"), [5, 6, 8, 7])
        store.set_deadline(self.source, self.state, 1, {"mode": "manual", "kind": "datetime",
                           "date": "2099-12-02", "time": "17:00"})
        self.assertEqual(ids("interested")[:2], [1, 2])

    def test_company_sort_uses_display_names_and_review_ties(self):
        names = ["㈜카카오", "(주) 가온", "주식회사 나무", "Acme", "acme", "123", None, ""]
        posts = []
        for i, name in enumerate(names, 1):
            post = posting(i)
            post["title"]["rendered"] = f"{name}｜개발" if name is not None else "개발"
            posts.append(post)
        self.collect(posts)
        for i in range(1, len(posts) + 1):
            self.review(i)
        rows = store.list_jobs(self.source, self.state, sort="company")["items"]
        self.assertEqual([row["id"] for row in rows], [2, 3, 1, 5, 4, 6, 8, 7])
        self.assertEqual(rows[0]["company"], "(주) 가온")

    def test_legacy_deadline_orders_by_stored_time_when_card_shows_only_date(self):
        self.collect([posting(2, content="<p>지원 마감: 2099-12-02 18:00</p>")])
        self.review(2)
        with sqlite3.connect(self.state) as db:
            db.execute("""INSERT INTO job_availability
                (post_id,closed,deadline_at,evidence,checked_at,source_modified_at,source_url)
                VALUES (1,0,'2099-12-02T03:00:00Z','','2026-11-17','2026-11-16','')""")
        rows = store.list_jobs(self.source, self.state, sort="deadline")["items"]
        self.assertEqual([(j["id"], j["deadline_label"]) for j in rows],
                         [(1, "12.02 마감"), (2, "12.02 18:00 마감")])

    def test_expired_sort_ignores_deadlines_later_than_confirmed_closure(self):
        early = posting(2, content="<p>지원 마감: 2020-11-20</p>")
        ordinary = posting(3, content="<p>지원 마감: 2020-11-19</p>")
        collected_closed = posting(4, content="<p>접수 종료</p><p>지원 마감: 2020-11-21</p>")
        early["modified_gmt"] = "2020-11-16T00:00:00Z"
        collected_closed["modified_gmt"] = "2020-11-16T00:00:00Z"
        self.collect([early, ordinary, collected_closed])
        for i in (2, 3, 4):
            self.review(i)
        early["categories"].append(collect_jobs.CLOSED)
        refresh(self.source, self.state, lambda _: ([early], 1), datetime(2020, 11, 16, tzinfo=timezone.utc))
        def ids():
            rows = store.list_jobs(self.source, self.state, view="expired", sort="deadline")["items"]
            return [job["id"] for job in rows]
        self.assertEqual(ids(), [3, 4, 2])
        refresh(self.source, self.state, lambda _: ([early], 1), datetime(2020, 11, 22, tzinfo=timezone.utc))
        self.assertEqual(ids(), [3, 4, 2])
        store.set_deadline(self.source, self.state, 2, {"mode": "manual", "kind": "date", "date": "2020-11-20"})
        self.assertEqual(ids(), [2, 3, 4])

    def test_deadline_sort_applies_to_all_matching_pages(self):
        self.collect([posting(i) for i in range(2, 32)])
        for i in range(2, 32):
            self.review(i)
        store.set_deadline(self.source, self.state, 1, {"mode": "manual", "kind": "date", "date": "2099-01-01"})
        self.assertEqual(store.list_jobs(self.source, self.state, page=2)["items"][0]["id"], 1)
        pages = [store.list_jobs(self.source, self.state, q="agent", sort="deadline", page=p)["items"]
                 for p in (1, 2)]
        self.assertEqual([len(page) for page in pages], [30, 1])
        self.assertEqual(pages[0][0]["id"], 1)
        self.assertEqual({job["id"] for page in pages for job in page}, set(range(1, 32)))

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
