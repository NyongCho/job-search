import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from job_search import collect_jobs, dashboard_store as store, refresh_job_availability as refreshing
from tests.test_collect_jobs import posting


def next_page(props):
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
        {"props": {"pageProps": props}}, ensure_ascii=False) + "</script>"


def ascent_page(positions, company="래티스(Lattice)"):
    companies = [{"id": 3216, "name": company, "positions": positions}]
    return '<script>self.__next_f.push(' + json.dumps([1, "45:" + json.dumps(
        ["$", "$L4d", None, {"companies": companies}], ensure_ascii=False) + "\n"],
        ensure_ascii=False) + ')</script>'


class SourceRefreshTests(unittest.TestCase):
    ascent_url = "https://www.ascent.me/jobs/freepass"
    fde = {"id": 20, "title": "FDE(Forward Deployed Engineer) 인턴", "conditions": ["2026.11.29 마감"]}
    staff = {"id": 21, "title": "CEO Staff 인턴", "conditions": ["2026.12.15 마감"]}
    cases = [
        ("greenhouse", 8044892003, "Moloco (몰로코)｜ML Engineer Intern",
         "https://job-boards.greenhouse.io/moloco/jobs/8044892003",
         "https://boards-api.greenhouse.io/v1/boards/moloco/jobs/8044892003?content=true",
         json.dumps({"id": 8044892003, "company_name": "Moloco", "title": "ML Engineer Intern",
                     "absolute_url": "https://job-boards.greenhouse.io/moloco/jobs/8044892003",
                     "application_deadline": "2026-12-11T14:59:00Z", "content": "<p>본문</p>"}), "12.11 23:59 마감"),
        ("Greeting", 217571, "팀카이｜FDE 인턴", "https://teamkai.career.greetinghr.com/ko/o/217571",
         "https://teamkai.career.greetinghr.com/ko/o/217571",
         next_page({"host": "teamkai.career.greetinghr.com", "openingId": "217571", "dehydratedState": {"queries": [
             {"queryKey": ["career", "getOpeningById", {"openingId": 217571}],
              "state": {"data": {"success": True, "data": {"groupInfo": {"name": "주식회사 팀카이"},
              "openingsInfo": {"openingId": "217571", "title": "FDE 인턴", "status": "OPEN",
                               "dueDate": "2026-12-12", "detail": "<p>본문</p>"}}}}}]}}), "12.12 마감"),
        ("Wanted", "398349", "일렉테리어｜AI 프로블럼 솔버", "https://www.wanted.co.kr/wd/398349",
         "https://www.wanted.co.kr/wd/398349",
         next_page({"initialData": {"id": 398349, "company": {"company_id": 43950, "company_name": "일렉테리어"},
                                    "position": "AI 프로블럼 솔버", "status": "active", "due_time": None,
                                    "intro": "본문"}}), "마감일 미확인"),
    ]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.sqlite"
        self.state = Path(self.temp.name) / "state.sqlite"
        self.now = datetime(2026, 11, 19, tzinfo=timezone.utc)
        collect_jobs.collect(self.jobs, lambda _: ([posting(1), posting(2)], 1), self.now)
        store.init_state_db(self.jobs, self.state)

    def document(self, local_id, source, job_id, title, url):
        with closing(sqlite3.connect(self.jobs)) as db, db:
            doc = json.loads(db.execute("SELECT document FROM posts WHERE id=?", (local_id,)).fetchone()[0])
            doc.update(source=source, source_job_id=job_id, title=title, url=url, apply_url=url + "/apply",
                       lines=["이전 본문"])
            doc.pop("modified_gmt", None)
            db.execute("UPDATE posts SET document=?,decision='recommend' WHERE id=?", (json.dumps(doc), local_id))

    def refresh(self, local_id, url, html, now=None):
        def fetch(actual, **_):
            self.assertEqual(actual, url)
            return actual, html.encode(), "text/html"
        return refreshing.refresh_one(self.jobs, self.state, local_id, fetch=fetch, now=now or self.now)

    def test_each_supported_platform_reads_selected_job_from_public_response(self):
        for source, job_id, title, url, fetch_url, body, label in self.cases:
            with self.subTest(source=source):
                self.document(1, source, job_id, title, url)
                self.refresh(1, fetch_url, body)
                job = store.get_job(self.jobs, self.state, 1)
                self.assertEqual(job["deadline_label"], label)
                self.assertIsNotNone(job["availability_checked_at"])

    def test_ascent_shared_url_uses_scope_and_exact_posting_id(self):
        self.document(1, "ascent", "20", "래티스(Lattice)｜" + self.fde["title"], self.ascent_url)
        self.document(2, "ascent", "21", "래티스(Lattice)｜" + self.staff["title"], self.ascent_url)
        self.refresh(1, self.ascent_url, ascent_page([self.fde, self.staff]))
        self.assertEqual(store.get_job(self.jobs, self.state, 1)["deadline_label"], "11.29 마감")
        self.assertIsNone(store.get_job(self.jobs, self.state, 2)["availability_checked_at"])
        self.refresh(1, self.ascent_url, ascent_page([self.staff, self.fde]))
        before = store.get_job(self.jobs, self.state, 1)
        self.assertEqual(before["deadline_label"], "11.29 마감")
        for body in [ascent_page([self.staff]), ascent_page([self.fde, self.fde]), ascent_page([self.fde], "다른 기업"),
                     "<title>로그인</title>", ""]:
            with self.assertRaises(ValueError):
                self.refresh(1, self.ascent_url, body)
            self.assertEqual(store.get_job(self.jobs, self.state, 1)["availability_checked_at"],
                             before["availability_checked_at"])
        extended = {**self.fde, "conditions": ["2026.12.22 마감"]}
        self.refresh(1, self.ascent_url, ascent_page([self.staff, extended]))
        self.assertEqual(store.get_job(self.jobs, self.state, 1)["deadline_label"], "12.22 마감")
        self.assertIsNone(store.get_job(self.jobs, self.state, 2)["availability_checked_at"])

    def test_refresh_can_finish_after_manual_save_without_overwriting_it(self):
        self.document(1, "ascent", "20", "래티스(Lattice)｜" + self.fde["title"], self.ascent_url)
        def fetch(url, **_):
            store.set_deadline(self.jobs, self.state, 1,
                               {"mode": "manual", "kind": "rolling", "word": "채용 시 마감"})
            return url, ascent_page([self.fde]).encode(), "text/html"
        refreshing.refresh_one(self.jobs, self.state, 1, fetch=fetch, now=self.now)
        job = store.get_job(self.jobs, self.state, 1)
        self.assertEqual(job["deadline_label"], "채용 시 마감")
        self.assertEqual(job["deadline_auto"]["label"], "11.29 마감")

    def test_wanted_uses_only_the_selected_pages_explicit_rolling_deadline_field(self):
        source, job_id, title, url, _, body, _ = self.cases[-1]
        self.document(1, source, job_id, title, url)
        body += '<article class="JobDueTime_JobDueTime__example"><h2>마감일</h2><span>상시채용</span></article>'
        self.refresh(1, url, body)
        self.assertEqual(store.get_job(self.jobs, self.state, 1)["deadline_label"], "상시 채용")

    def test_inthiswork_with_repurposed_identity_preserves_previous_facts(self):
        url = "https://inthiswork.com/wp-json/wp/v2/posts/1?" + refreshing.urlencode({"_fields": refreshing.FIELDS})
        original = posting(1, content="<p>지원 마감: 2099-11-27</p>")
        self.refresh(1, url, json.dumps(original))
        saved = store.get_job(self.jobs, self.state, 1)
        changed = posting(1, content="<p>상시 채용</p>")
        changed["title"]["rendered"] = "다른 기업｜엔지니어"
        with self.assertRaises(refreshing.SourceCheckFailed):
            self.refresh(1, url, json.dumps(changed), datetime(2026, 11, 20, tzinfo=timezone.utc))
        current = store.get_job(self.jobs, self.state, 1)
        self.assertEqual(current["deadline_label"], "11.27 마감")
        self.assertEqual(current["availability_checked_at"], saved["availability_checked_at"])
