"""Accept a private answer and subscribe the current job to a single fit evaluation."""
import json
import time
from contextlib import closing
from pathlib import Path

from . import refresh_queue
from .job_fit import jd_input, stable_hash


class AnswerChanged(Exception):
    """The submitted form no longer refers to an actionable question."""


def answer_statuses(db, post_id=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='review_answers'").fetchone():
        return {}
    query = """SELECT a.post_id,a.jd_version,a.question,w.status FROM review_answers a
        JOIN refresh_members m ON m.run_id=a.run_id
        JOIN refresh_work w ON w.group_id=m.group_id AND w.kind='fit' AND w.post_id=a.post_id"""
    args = () if post_id is None else (post_id,)
    if args:
        query += " WHERE a.post_id=?"
    result = {}
    for job, version, question, status in db.execute(query, args):
        result.setdefault(job, {}).setdefault(version, {})[question] = status
    return result


def restore_completed(jobs_path, state_path):
    """A rolled-back worker can republish a pre-answer score from its earlier cache."""
    from .job_fit import read_fit

    with closing(refresh_queue.connect(state_path)) as state, state:
        if not state.execute("SELECT 1 FROM sqlite_master WHERE name='review_answers'").fetchone():
            return
        if not state.execute("SELECT 1 FROM review_answers LIMIT 1").fetchone():
            return
        state.execute("ATTACH DATABASE ? AS jobs", (Path(jobs_path).resolve().as_uri() + "?mode=ro",))
        state.execute("BEGIN IMMEDIATE")
        pending = state.execute("""WITH latest AS (
            SELECT post_id,jd_version,max(run_id) AS run_id FROM review_answers GROUP BY post_id,jd_version)
            SELECT a.post_id,a.jd_version,a.run_id,w.id FROM latest a JOIN refresh_members m USING(run_id)
            JOIN refresh_work w USING(group_id) WHERE w.post_id=a.post_id AND w.kind='fit' AND w.status='done'""")
        for row in pending:
            post = state.execute("SELECT * FROM jobs.posts WHERE id=?", (row["post_id"],)).fetchone()
            if (post and stable_hash(jd_input(json.loads(post["document"]))) == row["jd_version"]
                    and (read_fit(post) or {}).get("answer_revision") != row["run_id"]):
                state.execute("UPDATE refresh_work SET status='queued',message='',ready_at=0 WHERE id=?", (row["id"],))


def submit(jobs_path, state_path, post_id, fingerprint, question, answer):
    from . import dashboard_store

    if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 2000:
        raise ValueError("답변은 1자 이상 2,000자 이하로 입력해 주세요.")
    if not isinstance(question, str) or not isinstance(fingerprint, str):
        raise ValueError("확인 질문과 공고 버전을 확인해 주세요.")
    answer = answer.strip()
    with closing(refresh_queue.connect(state_path)) as state, state:
        state.execute("ATTACH DATABASE ? AS jobs", (Path(jobs_path).resolve().as_uri() + "?mode=ro",))
        state.execute("BEGIN IMMEDIATE")
        job = dashboard_store.get_job(jobs_path, state_path, post_id)
        if not job:
            raise LookupError("공고를 찾을 수 없습니다.")
        if job["fingerprint"] != fingerprint or post_id <= 0:
            raise AnswerChanged("공고가 변경됐어요. 현재 질문을 다시 확인해 주세요.")
        current = state.execute("SELECT fingerprint,document FROM jobs.posts WHERE id=?", (post_id,)).fetchone()
        if current is None or current["fingerprint"] != fingerprint:
            raise AnswerChanged("공고가 변경됐어요. 현재 질문을 다시 확인해 주세요.")
        version = stable_hash(jd_input(json.loads(current["document"])))
        saved = state.execute("""SELECT answer,run_id FROM review_answers
            WHERE post_id=? AND jd_version=? AND question=?""", (post_id, version, question)).fetchone()
        if saved and saved["answer"] == answer:
            return saved["run_id"]
        if (job["review_route"] != "question" or question not in job["review_questions"]
                or job["status"] in {"applied", "archived"}):
            raise AnswerChanged("이 질문의 평가 상태가 변경됐어요. 공고를 다시 확인해 주세요.")
        # A distinct group version keeps a concurrent full refresh's deadline and logo work independent.
        scope = f"detail:{post_id}"
        run = state.execute("INSERT INTO refresh_runs(scope,signature,created) VALUES(?,?,?)",
                            (scope, version, time.time())).lastrowid
        group = state.execute("INSERT INTO refresh_groups(post_id,version) VALUES(?,?)",
                              (post_id, "answer:" + version)).lastrowid
        state.execute("INSERT INTO refresh_members VALUES(?,?)", (run, group))
        state.execute("INSERT INTO refresh_work(group_id,post_id,kind) VALUES(?,?,'fit')", (group, post_id))
        state.execute("INSERT OR REPLACE INTO review_answers(post_id,jd_version,question,answer,run_id) VALUES(?,?,?,?,?)",
                      (post_id, version, question, answer, run))
        return run
