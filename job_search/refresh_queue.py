"""Durable subscriptions to independently committed deadline, logo and fit work."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

KINDS = ("deadline", "logo", "fit")
STATUSES = ("queued", "running", "done", "failed", "skipped")
LOGO_OUT_OF_SCOPE = "로고는 추천·관심·지원 준비·지원한 공고에서 수집해요."
FIT_FAILURE_REASONS = {
    "Hermes 평가 실행 준비 실패": "Hermes가 적합도 평가를 시작하지 못했어요.",
    "평가 응답에서 공고 누락 또는 중복": "평가 응답에서 이 공고가 누락되거나 중복됐어요.",
    "Invalid fit tenure months or required_months": "평가 응답의 경력 개월 수가 올바르지 않아요.",
    "Fit months are only valid for tenure": "평가 응답에 경력 기간과 무관한 항목의 개월 수가 들어 있어요.",
    "Fit tenure must be a required criterion": "평가 응답에서 필수 경력 기간이 잘못 분류됐어요.",
    "Invalid fit criterion evidence_lines": "평가 기준이 인용한 공고 본문의 행을 확인할 수 없어요.",
    "Invalid fit criterion category or status": "평가 기준의 분류 또는 판정이 올바르지 않아요.",
    "Invalid fit assessment version or JD line count": "평가 형식 버전이나 공고 본문의 행 수가 올바르지 않아요.",
    "Invalid fit assessment completeness, preference, eligibility or criteria": "평가 응답의 지원 요건·선호도·평가 기준을 확인할 수 없어요.",
    "Fit question must be a single line": "평가 응답의 확인 질문이 여러 줄로 나뉘어 있어요.",
    "A question is required for unknown required experience": "필수 경험을 확인하기 위한 질문이 평가 응답에서 빠졌어요.",
    "Invalid fit question": "평가 응답의 확인 질문 형식이 올바르지 않아요.",
    "평가 실행 확인 필요": "평가 실행 결과를 확인할 수 없어요.",
    "JD 버전 불일치": "평가한 공고의 버전이 요청한 버전과 달라요.",
    "지원 판정 누락": "평가 응답에 지원 판정이 없어요.",
    "판정 요약 누락": "평가 응답에 판정 요약이 없어요.",
    "JD 근거 행 오류": "평가가 인용한 공고 본문의 행을 확인할 수 없어요.",
    "JD 인용문 불일치": "평가가 인용한 문장이 공고 본문과 일치하지 않아요.",
    "경력 근거 불일치": "평가가 인용한 경력 근거를 확인할 수 없어요.",
}


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def create_tables(db):
    for statement in (
        """CREATE TABLE IF NOT EXISTS refresh_runs (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL, signature TEXT NOT NULL, created REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS review_answers (
            post_id INTEGER NOT NULL, jd_version TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
            run_id INTEGER NOT NULL REFERENCES refresh_runs(id), PRIMARY KEY(post_id,jd_version,question))""",
        """CREATE TABLE IF NOT EXISTS refresh_receipts (
            request_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES refresh_runs(id))""",
        """CREATE TABLE IF NOT EXISTS refresh_groups (
            id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, version TEXT NOT NULL, applied_version TEXT)""",
        """CREATE INDEX IF NOT EXISTS refresh_group_version ON refresh_groups(post_id,version,id)""",
        """CREATE TABLE IF NOT EXISTS refresh_work (
            id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES refresh_groups(id),
            post_id INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            ready_at REAL NOT NULL DEFAULT 0, recheck INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
            fit_run_id INTEGER REFERENCES refresh_runs(id), UNIQUE(group_id,kind))""",
        """CREATE INDEX IF NOT EXISTS refresh_pending ON refresh_work(kind,status,ready_at,id)""",
        """CREATE TABLE IF NOT EXISTS refresh_members (
            run_id INTEGER NOT NULL REFERENCES refresh_runs(id), group_id INTEGER NOT NULL REFERENCES refresh_groups(id),
            PRIMARY KEY(run_id,group_id))""",
        """CREATE INDEX IF NOT EXISTS refresh_subscribers ON refresh_members(group_id,run_id)""",
        """CREATE TABLE IF NOT EXISTS refresh_changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, work_id INTEGER NOT NULL UNIQUE REFERENCES refresh_work(id))""",
        """CREATE INDEX IF NOT EXISTS refresh_scope ON refresh_runs(scope,id)""",
    ):
        db.execute(statement)
    if "fit_run_id" not in {row[1] for row in db.execute("PRAGMA table_info(refresh_work)")}:
        db.execute("ALTER TABLE refresh_work ADD COLUMN fit_run_id INTEGER REFERENCES refresh_runs(id)")


def _recheck_fits(db, run):
    # The web process never reads career files. Ask the same work item to consult its input cache
    # after an explicit new click, including one received while an older answer was being scored.
    db.execute("""UPDATE refresh_work SET fit_run_id=CASE WHEN recheck=0 THEN ? ELSE fit_run_id END,
        recheck=(status='running'),
        status=CASE WHEN status='running' THEN status ELSE 'queued' END
        WHERE kind='fit' AND status<>'queued' AND group_id IN
        (SELECT group_id FROM refresh_members WHERE run_id=?)""", (run, run))


def _scope_logos(db, run, logo_post_ids):
    if logo_post_ids is None:
        return
    # A second click can subscribe to an active group after the job was moved to another tab.
    changed = db.execute("""WITH eligible AS (SELECT value FROM json_each(?))
        UPDATE refresh_work SET
            status=CASE WHEN status='queued' THEN 'skipped' ELSE 'queued' END,
            message=CASE WHEN status='queued' THEN ? ELSE '' END, ready_at=0
        WHERE kind='logo' AND post_id>0 AND group_id IN (SELECT group_id FROM refresh_members WHERE run_id=?)
          AND ((status='queued' AND post_id NOT IN eligible)
            OR (status='skipped' AND message=? AND post_id IN eligible))
        RETURNING id""", (json.dumps(sorted(logo_post_ids)), LOGO_OUT_OF_SCOPE, run, LOGO_OUT_OF_SCOPE)).fetchall()
    db.executemany("INSERT OR REPLACE INTO refresh_changes(work_id) VALUES(?)", changed)


def enqueue(path, targets, scope, request_id=None, *, logo_post_ids=None):
    encoded = json.dumps(sorted(set(map(tuple, targets))))
    signature = hashlib.sha256(encoded.encode()).hexdigest()
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if request_id:
            saved = db.execute("""SELECT r.id,r.scope FROM refresh_runs r JOIN refresh_receipts t ON t.run_id=r.id
                                  WHERE t.request_id=?""",(request_id,)).fetchone()
            if saved:
                if saved["scope"] != scope:
                    raise ValueError("이 요청 번호는 다른 새로고침에 사용됐어요.")
                return saved["id"]
        existing = db.execute("""SELECT r.id FROM refresh_runs r WHERE scope=? AND signature=? AND EXISTS(
            SELECT 1 FROM refresh_members m JOIN refresh_work w USING(group_id)
            WHERE m.run_id=r.id AND w.status IN ('queued','running')) ORDER BY r.id DESC LIMIT 1""",
                              (scope, signature)).fetchone()
        if existing:
            _scope_logos(db, existing[0], logo_post_ids)
            _recheck_fits(db, existing[0])
            if request_id:
                db.execute("INSERT INTO refresh_receipts VALUES(?,?)", (request_id, existing[0]))
            return existing[0]
        run = db.execute("INSERT INTO refresh_runs(scope,signature,created) VALUES(?,?,?)",
                         (scope, signature, time.time())).lastrowid
        if request_id:
            db.execute("INSERT INTO refresh_receipts VALUES(?,?)", (request_id, run))
        # Resolve all overlapping jobs against the same pre-insert snapshot.
        db.execute("""CREATE TEMP TABLE targets AS
            WITH requested AS (
                SELECT json_extract(value,'$[0]') AS post_id, json_extract(value,'$[1]') AS version
                FROM json_each(?))
            SELECT post_id, version, (
                SELECT g.id FROM refresh_groups g WHERE g.post_id=t.post_id
                AND (g.version=t.version OR g.applied_version=t.version) AND EXISTS (
                    SELECT 1 FROM refresh_work w WHERE w.group_id=g.id AND w.status IN ('queued','running'))
                ORDER BY g.id DESC LIMIT 1) AS active_group FROM requested t""", (encoded,))
        created = db.execute("""INSERT INTO refresh_groups(post_id,version)
            SELECT post_id,version FROM targets WHERE active_group IS NULL RETURNING id,post_id""").fetchall()
        if created:
            new_groups = json.dumps([tuple(row) for row in created])
            db.execute("""INSERT INTO refresh_work(group_id,post_id,kind)
                SELECT json_extract(g.value,'$[0]'),json_extract(g.value,'$[1]'),k.value
                FROM json_each(?) g CROSS JOIN json_each(?) k""", (new_groups, json.dumps(KINDS)))
            db.execute("""INSERT INTO refresh_members(run_id,group_id)
                SELECT ?,json_extract(value,'$[0]') FROM json_each(?)""", (run, new_groups))
        db.execute("""INSERT INTO refresh_members(run_id,group_id)
            SELECT DISTINCT ?,active_group FROM targets WHERE active_group IS NOT NULL""", (run,))
        _scope_logos(db, run, logo_post_ids)
        _recheck_fits(db, run)
        return run


def claim(path, kind, limit, *, now=None):
    if kind not in KINDS or type(limit) is not int or limit < 1:
        raise ValueError("Invalid refresh claim")
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        tasks = db.execute("""SELECT w.id,w.post_id,g.version,w.kind,d.id AS source_work_id,d.status AS source_status,
            coalesce(w.fit_run_id,(SELECT min(run_id) FROM refresh_members m WHERE m.group_id=g.id)) AS run_id
            FROM refresh_work w JOIN refresh_groups g ON g.id=w.group_id
            LEFT JOIN refresh_work d ON d.group_id=g.id AND d.kind='deadline'
            WHERE w.kind=? AND w.status='queued' AND w.ready_at<=?
            AND (w.kind<>'fit' OR d.id IS NULL OR d.status NOT IN ('queued','running'))
            ORDER BY w.ready_at,w.id LIMIT ?""", (kind, time.time() if now is None else now, limit)).fetchall()
        db.executemany("UPDATE refresh_work SET status='running' WHERE id=?", ((t["id"],) for t in tasks))
        return [dict(t) for t in tasks]


def reset_running(path):
    # The service holds a per-state-DB process lock before recovering an interrupted owner.
    with closing(connect(path)) as db, db:
        db.execute("UPDATE refresh_work SET status='queued',ready_at=0,recheck=0 WHERE status='running'")


def defer(path, task):
    with closing(connect(path)) as db, db:
        db.execute("UPDATE refresh_work SET status='queued',ready_at=? WHERE id=? AND status='running'",
                   (time.time() + 0.5, task["id"]))


def progress(db, task):
    db.execute("""INSERT OR REPLACE INTO refresh_changes(work_id)
        SELECT id FROM refresh_work WHERE id=? AND status='running'""", (task["id"],))


def finish(path, task, status, message="", *, db=None):
    if status not in ("done", "failed", "skipped"):
        raise ValueError("Invalid refresh result")
    if db is None:
        with closing(connect(path)) as handle, handle:
            return finish(path, task, status, message, db=handle)
    updated = db.execute("""UPDATE refresh_work SET status=CASE WHEN recheck=1 THEN 'queued' ELSE ? END,
                         recheck=0,message=? WHERE id=? AND status='running'""",
                         (status, message, task["id"]))
    if updated.rowcount:
        if task.get("applied_version"):
            db.execute("UPDATE refresh_groups SET applied_version=? WHERE id=(SELECT group_id FROM refresh_work WHERE id=?)",
                       (task["applied_version"], task["id"]))
        db.execute("INSERT OR REPLACE INTO refresh_changes(work_id) VALUES(?)", (task["id"],))


def _result_message(kind, status, message):
    if status in ("queued", "running"):
        return ""
    if kind == "fit" and status == "failed":
        # Old fit failures can contain raw model output or an exception from the evaluator.
        if message.startswith("Conflicting fit evidence for "):
            return "같은 평가 기준에 서로 다른 판정이 기록됐어요."
        if message in {"Invalid fit criterion", "Invalid fit key", "Invalid fit label", "Invalid fit category",
                       "Invalid fit status", "Invalid fit evidence"}:
            return "평가 기준의 이름·판정·근거 형식을 확인할 수 없어요."
        return FIT_FAILURE_REASONS.get(message, "적합도 평가를 완료하지 못해 점수를 갱신하지 못했어요.")
    return message


def _snapshot(db, run, after, post_id=None):
    meta = db.execute("SELECT scope FROM refresh_runs WHERE id=?", (run,)).fetchone()
    if meta is None:
        raise LookupError("Unknown refresh")
    counts = dict.fromkeys(STATUSES, 0)
    by_kind = {kind: dict.fromkeys(STATUSES, 0) for kind in KINDS}
    for kind, status, count in db.execute("""SELECT kind,status,count(*) FROM refresh_members m
            JOIN refresh_work w USING(group_id) WHERE run_id=? GROUP BY kind,status""", (run,)):
        by_kind[kind][status] = count
        counts[status] += count
    query = """ FROM refresh_changes c JOIN refresh_work w ON w.id=c.work_id
        JOIN refresh_members m ON m.group_id=w.group_id WHERE m.run_id=?"""
    cursor = db.execute("SELECT coalesce(max(seq),0)" + query, (run,)).fetchone()[0]
    changes = [] if after is None else [dict(r) for r in db.execute(
        "SELECT seq,w.post_id,w.kind,w.status,substr(w.message,1,256) AS message" + query
        + " AND seq>? ORDER BY seq", (run, after))]
    for change in changes:
        change["message"] = _result_message(change["kind"], change["status"], change["message"])
    result = {"id": run, "counts": counts, "by_kind": by_kind, "cursor": cursor, "changes": changes,
              "list_scope": meta["scope"].startswith("list:"), "complete": not (counts["queued"] or counts["running"])}
    if post_id is not None:
        result["post_tasks"] = {
            kind: {"status": status, "message": _result_message(kind, status, message)}
            for kind, status, message in db.execute("""SELECT w.kind,w.status,substr(w.message,1,256)
                FROM refresh_work w JOIN refresh_members m USING(group_id)
                WHERE m.run_id=? AND w.post_id=?""", (run, post_id))
        }
    return result


def snapshot(path, run, after=None, *, post_id=None):
    with closing(connect(path)) as db:
        db.execute("BEGIN")
        return _snapshot(db, run, after, post_id)


def activity(path):
    # A job outside this page can enter its tab or change the sidebar totals.
    with closing(connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='refresh_changes'").fetchone():
            return {"cursor": 0, "active": False}
        row = db.execute("""SELECT (SELECT coalesce(max(seq),0) FROM refresh_changes),
            EXISTS(SELECT 1 FROM refresh_work WHERE kind IN ('deadline','logo','fit')
                   AND status IN ('queued','running'))""").fetchone()
        return {"cursor": row[0], "active": bool(row[1])}


def find(path, scope, targets, *, post_id=None):
    # One consistent read covers navigation during a commit on either side of a page boundary.
    with closing(connect(path)) as db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='refresh_runs'").fetchone():
            return []
        if post_id is not None:
            # The newest refresh that included this job can be individual or list-wide.
            run = db.execute("""SELECT max(m.run_id) FROM refresh_groups g
                JOIN refresh_members m ON m.group_id=g.id WHERE g.post_id=?""", (post_id,)).fetchone()[0]
            return [_snapshot(db, run, None, post_id)] if run is not None else []
        candidates = db.execute("""SELECT r.id,r.scope, EXISTS(SELECT 1 FROM refresh_members m
            JOIN refresh_work w USING(group_id) WHERE m.run_id=r.id AND w.status IN ('queued','running')) AS active
            FROM refresh_runs r WHERE r.scope=? OR EXISTS(SELECT 1 FROM refresh_members m
            JOIN refresh_groups g ON g.id=m.group_id WHERE m.run_id=r.id AND g.post_id IN
            (SELECT value FROM json_each(?))) ORDER BY r.id DESC""", (scope, json.dumps(targets)))
        ids = []
        exact = newest = None
        for row in candidates:
            run = row["id"]
            if newest is None:
                newest = run
            if exact is None and row["scope"] == scope:
                exact = run
            if row["active"] or run == exact:
                ids.append(run)
        if not ids and newest is not None:
            ids.append(newest)
        return [_snapshot(db, run, None) for run in ids]


def receipt(path, request_id):
    with closing(connect(path)) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT run_id FROM refresh_receipts WHERE request_id=?", (request_id,)).fetchone()
        return [_snapshot(db, row[0], None)] if row else []


def fit_work_ids(path, run):
    with closing(connect(path)) as db:
        return [r[0] for r in db.execute("""SELECT w.id FROM refresh_work w JOIN refresh_members m USING(group_id)
            WHERE run_id=? AND kind='fit'""", (run,))]
