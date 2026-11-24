import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from job_search import collect_jobs, dashboard_store, fit_work, poster_evidence
from tests.test_collect_jobs import posting
from tests.test_fit_work import response


class PosterScheduleTests(unittest.TestCase):
    def setUp(self):
        self.jobs = Path(self.enterContext(tempfile.TemporaryDirectory())) / "jobs.sqlite"
        self.state = self.jobs.with_name("state.sqlite")
        self.profile = {"P1": "필수 경험"}
        self.prepared, self.assessed = [], []
        self.reader = self.enterContext(patch.object(poster_evidence, "read_poster", side_effect=self.read))
        self.fetch = self.enterContext(patch.object(poster_evidence, "fetch_public",
                                                    side_effect=lambda url, **_: (url, url.encode(), "image/png")))
        self.model = self.enterContext(patch.object(poster_evidence, "read_poster_model",
                                                    side_effect=AssertionError("정기 모델 판독")))

    def seed(self, poster_ids, *, closed=(), unknown=(), text=(), delivered=()):
        posts = []
        for i in [*poster_ids, *text]:
            body = ("<p>모집 안내</p><img src=\"https://example.org/poster-%d.png\">" % i
                    if i in poster_ids else "<p>필수 경력 Python 서비스 개발</p>")
            if i not in unknown:
                date = "2020-01-01" if i in closed else "2099-12-31"
                body += f"<p>지원 마감 {date}</p>"
            posts.append(posting(i, body + f'<a href="https://example.org/apply/{i}">지원하러 가기</a>'))
        collect_jobs.collect(self.jobs, lambda _: (posts, 1))
        dashboard_store.init_state_db(self.jobs, self.state)
        fit_work.migrate(self.jobs, self.state, profile=self.profile)
        with closing(sqlite3.connect(self.jobs)) as db, db:
            db.executemany("UPDATE fit_delivery SET execution_id='legacy' WHERE post_id=?", [(i,) for i in delivered])
            db.execute("INSERT OR IGNORE INTO outbox VALUES('original','기존 수신 내용','[]',1)")

    def read(self, data):
        i = int(data.decode().rsplit("-", 1)[1].split(".")[0])
        self.prepared.append(i)
        return {"status": "ready", "reason": "ocr_ready", "lines": [
            {"text": f"직무 {i} 필수 업무는 서비스 운영입니다.", "bbox": [5, 25, 450, 32]}]}

    def run_schedule(self, name, **kwargs):
        def evaluate(a, p, docs):
            self.assessed.extend(d["id"] for d in docs)
            return response(a, p, docs)
        return fit_work.scheduled_review(self.jobs, self.state, name, profile=self.profile,
                                         runner=evaluate, verifier=lambda _: {}, **kwargs)

    def test_normal_runs_budget_old_and_new_posters_together_across_retries(self):
        self.seed([1, 2, 3, 4, 5, 6], closed=[1], unknown=[2], delivered=[3, 5], text=[7])
        first = self.run_schedule("day")
        self.assertEqual(self.prepared, [3, 4])
        self.assertEqual(set(self.assessed), {3, 4, 7})
        self.assertEqual(first.count("100.0점"), 2)
        self.assertEqual(self.run_schedule("day"), first)
        self.assertEqual(self.prepared, [3, 4])
        self.assertEqual(self.assessed.count(7), 1)
        self.run_schedule("evening")
        self.assertEqual(self.prepared, [3, 4, 5, 6])
        self.run_schedule("next-day")
        self.assertEqual(self.prepared, [3, 4, 5, 6, 2])
        self.model.assert_not_called()
        with sqlite3.connect(self.jobs) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'poster_schedule:%'").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT id,digest,delivered FROM outbox WHERE id='original'").fetchone(),
                             ("original", "기존 수신 내용", 1))
            self.assertEqual(db.execute("SELECT execution_id FROM fit_delivery WHERE post_id IN (3,5)").fetchall(),
                             [("legacy",), ("legacy",)])

    def test_interrupted_execution_keeps_the_reserved_pair_when_the_candidate_order_changes(self):
        self.seed([2, 3, 4, 5])
        from job_search.poster_review import prepare_post
        def interrupted(path, state, post_id, **kwargs):
            if post_id == 3:
                raise KeyboardInterrupt
            return prepare_post(path, state, post_id, **kwargs)
        with patch("job_search.poster_review.prepare_post", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.run_schedule("interrupted")
        self.assertEqual(self.prepared, [2])
        self.seed([1])
        self.run_schedule("interrupted")
        self.assertEqual(self.prepared, [2, 3])
        self.run_schedule("next")
        self.assertEqual(self.prepared, [2, 3, 1, 4])

    def test_unreadable_selected_poster_does_not_consume_an_extra_slot_or_block_text(self):
        self.seed([1, 2, 3], text=[4])
        from job_search.poster_review import prepare_post
        def unreadable(path, state, post_id, **kwargs):
            if post_id == 1:
                raise OSError("포스터 저장소를 일시적으로 읽을 수 없음")
            return prepare_post(path, state, post_id, **kwargs)
        with patch("job_search.poster_review.prepare_post", side_effect=unreadable):
            digest = self.run_schedule("failure")
        self.assertEqual(self.prepared, [2])
        self.assertEqual(set(self.assessed), {2, 4})
        self.assertEqual(digest.count("100.0점"), 2)

    def test_delivery_retry_after_model_completion_consumes_no_more_reads_or_fit_calls(self):
        self.seed([1, 2, 3], unknown=[1, 2, 3])
        def evaluate(a, p, docs):
            self.assessed.extend(d["id"] for d in docs)
            return response(a, p, docs)
        def interrupt(_):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            fit_work.scheduled_review(self.jobs, self.state, "replay", profile=self.profile,
                                      runner=evaluate, verifier=interrupt)
        self.assertEqual(self.assessed, [1, 2])
        self.run_schedule("replay")
        self.assertEqual(self.prepared, [1, 2])
        self.assertEqual(self.assessed, [1, 2])

    def test_old_poster_backlog_does_not_hide_new_text_after_the_100_target_limit(self):
        self.seed(range(1, 105), text=[200])
        self.run_schedule("backlog")
        self.assertEqual(self.prepared, [1, 2])
        self.assertEqual(set(self.assessed), {1, 2, 200})

    def test_malformed_unrelated_old_record_does_not_stop_a_regular_run(self):
        self.seed([1, 2], text=[4])
        with sqlite3.connect(self.jobs) as db:
            db.execute("UPDATE posts SET document=? WHERE id=1", ("{broken",))
        self.run_schedule("legacy")
        self.assertEqual(self.prepared, [2])
        self.assertEqual(set(self.assessed), {2, 4})

    def test_explicit_backfill_keeps_its_fixed_ids(self):
        self.seed([1, 2, 3, 4])
        self.run_schedule("fixed", ids=[4, 3, 2])
        self.assertEqual(self.prepared, [4, 3, 2])
