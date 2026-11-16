import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import collect_jobs
import dashboard_store as store
from job_availability import availability, inspect_document
from refresh_job_availability import refresh
from test_collect_jobs import posting


def document(*lines, published="2026-11-01T02:15:44"):
    return {"lines": list(lines), "categories": [], "published_gmt": published,
            "modified_gmt": published, "url": "https://inthiswork.com/archives/1"}


class DeadlineTests(unittest.TestCase):
    def test_explicit_application_deadlines(self):
        cases = [
            (["지원 접수 마감: 11/15 (일) 23:59"], "2026-11-15T23:59:59+09:00"),
            (["마감기한", "2026년 11월 15일, 23:59"], "2026-11-15T23:59:59+09:00"),
            (["입사지원 : ~2026-11-15(일) 23:59 까지"], "2026-11-15T23:59:59+09:00"),
            (["접수기간", "2026.11.09(월) 10:30 ~ 2026.11.25(수) 23:59"], "2026-11-25T23:59:59+09:00"),
            (["11월 22일 오후 11시 마감"], "2026-11-22T23:00:59+09:00"),
            (["접수 마감: 2026년 11월 23일 월요일 오후 11시 59분"], "2026-11-23T23:59:59+09:00"),
            (["지원 마감: 2026-11-16"], "2026-11-16T23:59:59+09:00"),
        ]
        for lines, expected in cases:
            with self.subTest(lines=lines):
                self.assertEqual(inspect_document(document(*lines))["deadline_at"], expected)

    def test_no_work_assessment_rolling_or_ambiguous_deadlines(self):
        for lines in [
            ["근무기간: 채용시 ~ 2026-11-01"], ["과제 제출 마감: 2026-11-01"],
            ["접수마감", "채용 시 마감", "근무기간: 2026-11-01 ~ 2026-12-01"],
            ["마감일은 추후 안내", "결과 발표: 2026-11-01"],
            ["지원 마감: 2026-11-01", "지원 마감: 2026-11-25"],
            ["지원 마감: 2026-11-31"], ["채용 완료 시 조기 마감될 수 있습니다."],
        ]:
            with self.subTest(lines=lines):
                result = inspect_document(document(*lines))
                self.assertIsNone(result["deadline_at"])
                self.assertFalse(result["closed"])

    def test_yearless_dates_require_publication_and_roll_december_forward(self):
        self.assertEqual(inspect_document(document("지원 마감: 1/10", published="2026-12-20T00:00:00"))["deadline_at"],
                         "2027-01-10T23:59:59+09:00")
        self.assertIsNone(inspect_document(document("지원 마감: 11/15", published=None))["deadline_at"])
        self.assertIsNone(inspect_document(document("지원 마감: 10/15"))["deadline_at"])

    def test_expiry_at_kst_boundary_and_newer_document_beats_cached_closure(self):
        doc = document("지원 마감: 2026-11-15")
        self.assertFalse(availability(doc, now=datetime(2026, 11, 15, 14, 59, 59, tzinfo=timezone.utc))["is_expired"])
        self.assertTrue(availability(doc, now=datetime(2026, 11, 15, 15, tzinfo=timezone.utc))["is_expired"])
        cached = {**inspect_document(doc), "closed": True, "checked_at": "2026-11-02T00:00:00+00:00",
                  "source_modified_at": "2026-11-01T00:00:00+00:00"}
        doc.update(modified_gmt="2026-11-16T00:00:00", lines=["지원 마감: 2026-12-01"])
        self.assertFalse(availability(doc, cached, datetime(2026, 11, 16, tzinfo=timezone.utc))["is_expired"])

    def test_24_hour_deadline_is_serializable_and_expires_after_midnight(self):
        doc = document('지원 마감: 2026-11-15 24:00')
        self.assertEqual(inspect_document(doc)['deadline_at'], '2026-11-16T00:00:00+09:00')
        self.assertFalse(availability(doc, now=datetime(2026, 11, 15, 14, 59, 59, tzinfo=timezone.utc))['is_expired'])
        self.assertTrue(availability(doc, now=datetime(2026, 11, 15, 15, 0, 1, tzinfo=timezone.utc))['is_expired'])

    def test_assessment_deadlines_do_not_close_applications(self):
        for line in ['코딩테스트 응시 마감: 2026-11-15', '온라인 인적성 검사 응시 마감: 2026-11-15',
                     'Coding test closing date: 2026-11-15']:
            with self.subTest(line=line):
                result = availability(document(line), now=datetime(2026, 11, 16, tzinfo=timezone.utc))
                self.assertIsNone(result['deadline_at'])
                self.assertFalse(result['is_expired'])


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "jobs.sqlite"
        self.state = Path(self.temp.name) / "private/state.sqlite"
        self.now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        collect_jobs.collect(self.source, lambda _: ([posting(1), posting(2)], 1), self.now)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE posts SET decision='recommend',reason='fit',evidence_lines='[1]'")
        store.init_state_db(self.source, self.state)

    def test_refresh_closure_extension_and_personal_state_preservation(self):
        store.set_status(self.source, self.state, 1, "applied")
        store.set_stage(self.source, self.state, 1, "document", "passed", "", "keep")
        store.set_status(self.source, self.state, 2, "interested")
        source_bytes = self.source.read_bytes()
        closed = posting(1); closed["categories"].append(collect_jobs.CLOSED)
        overdue = posting(2, content="<p>지원 마감: 2026-11-15</p>")
        refresh(self.source, self.state, lambda _: ([closed, overdue], 1), self.now)
        self.assertEqual(store.list_jobs(self.source, self.state)["total"], 0)
        self.assertEqual(store.list_jobs(self.source, self.state, view="expired")["total"], 1)
        self.assertEqual(store.list_jobs(self.source, self.state, view="interested")["total"], 1)
        applied = store.get_job(self.source, self.state, 1)
        self.assertTrue(applied["is_expired"])
        self.assertEqual(applied["progress"]["document"]["note"], "keep")
        future = posting(2, content="<p>지원 마감: 2099-11-15</p>")
        refresh(self.source, self.state, lambda _: ([posting(1), future], 1), self.now)
        self.assertFalse(store.get_job(self.source, self.state, 1)["is_expired"])
        self.assertEqual(store.list_jobs(self.source, self.state)["total"], 1)
        self.assertEqual(self.source.read_bytes(), source_bytes)

    def test_missing_or_failed_source_never_invents_closure_or_loses_last_check(self):
        closed = posting(1); closed["categories"].append(collect_jobs.CLOSED)
        refresh(self.source, self.state, lambda _: ([closed], 1), self.now)
        self.assertFalse(store.get_job(self.source, self.state, 2)["is_expired"])
        with closing(sqlite3.connect(self.state)) as db:
            before = list(db.iterdump())
        def failed(_): raise OSError("offline")
        with self.assertRaises(OSError):
            refresh(self.source, self.state, failed, self.now)
        refresh(self.source, self.state, lambda _: ([], 1), self.now)
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(list(db.iterdump()), before)

    def test_saved_deadline_expires_without_refresh_and_get_does_not_write(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            doc = json.loads(db.execute('SELECT document FROM posts WHERE id=1').fetchone()[0])
            doc['lines'] = ['지원 마감: 2020-11-15']
            db.execute('UPDATE posts SET document=? WHERE id=1', (json.dumps(doc),))
        before = self.state.read_bytes()
        self.assertTrue(store.get_job(self.source, self.state, 1)['is_expired'])
        self.assertEqual(store.list_jobs(self.source, self.state, view='expired')['items'][0]['id'], 1)
        self.assertEqual(before, self.state.read_bytes())

    def test_invalid_response_and_later_batch_failure_are_atomic(self):
        collect_jobs.collect(self.source, lambda _: ([posting(i) for i in range(3, 102)], 1), self.now)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE posts SET decision='recommend'")
        before = self.state.read_bytes()
        calls = []
        def second_batch_fails(url):
            calls.append(url)
            if len(calls) == 2:
                raise OSError('second batch failed')
            return [posting(1, categories=[collect_jobs.CLOSED])], 1
        with self.assertRaises(OSError):
            refresh(self.source, self.state, second_batch_fails, self.now)
        self.assertEqual(self.state.read_bytes(), before)
        for response in [[posting(999)], [posting(1), posting(1)], [posting(1, categories=['bad'])], {}]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                refresh(self.source, self.state, lambda _: (response, 1), self.now)
            self.assertEqual(self.state.read_bytes(), before)

    def test_malformed_timestamps_cannot_erase_previous_deadline(self):
        collect_jobs.collect(self.source, lambda _: ([posting(i) for i in range(3, 102)], 1), self.now)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE posts SET decision='recommend'")
        refresh(self.source, self.state, lambda url: ([posting(1, content='<p>지원 마감: 2020-11-15</p>')]
                if 'include=1%2C' in url else [], 1), self.now)
        before = self.state.read_bytes()
        for field in ['date_gmt', 'modified_gmt']:
            for later in [False, True]:
                calls = []
                def malformed(url):
                    calls.append(url)
                    post = posting(1 if len(calls) == 1 else 101)
                    if not later or len(calls) == 2:
                        post[field] = 'not-a-date'
                    return [post], 1
                with self.subTest(field=field, later=later), self.assertRaises(ValueError):
                    refresh(self.source, self.state, malformed, self.now)
                self.assertEqual(self.state.read_bytes(), before)
                self.assertTrue(store.get_job(self.source, self.state, 1)['is_expired'])

    def test_other_platform_ids_are_not_sent_to_inthiswork(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            doc = json.loads(db.execute('SELECT document FROM posts WHERE id=2').fetchone()[0])
            doc.update(source='greenhouse', url='https://job-boards.greenhouse.io/example/jobs/2')
            db.execute('UPDATE posts SET document=? WHERE id=2', (json.dumps(doc),))
        urls = []
        def fetch(url):
            urls.append(url)
            return [posting(1)], 1
        report = refresh(self.source, self.state, fetch, self.now)
        from urllib.parse import parse_qs, urlsplit
        self.assertEqual(parse_qs(urlsplit(urls[0]).query)['include'], ['1'])
        self.assertEqual(report['unsupported_ids'], [2])
        self.assertFalse(store.get_job(self.source, self.state, 2)['is_expired'])


if __name__ == '__main__':
    unittest.main()
