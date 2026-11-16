"""Read-only recommendation views and independent personal application state."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .job_availability import availability

STATUSES = {"unmarked", "interested", "preparing", "applied", "archived"}
VIEWS = ("recommended", "interested", "preparing", "applied", "archived", "needs_review", "expired")
PAGE_SIZE = 30
STAGE_LABELS = {"document": "서류", "interview1": "1차 면접", "interview2": "2차 면접", "final": "최종 결과"}
RESULT_LABELS = {"unknown": "미확인", "not_started": "미진행", "in_progress": "진행 중",
                 "passed": "합격", "failed": "불합격", "skipped": "해당 없음"}


def timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        return None


def safe_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    if any(ord(c) < 32 or c == "\\" for c in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port  # Reject malformed ports before presenting a link.
        return value.strip()
    except ValueError:
        return None


def _validate_paths(jobs_db, state_db):
    source, state = Path(jobs_db).resolve(), Path(state_db).resolve()
    if not source.is_file():
        raise FileNotFoundError("공고 데이터베이스를 찾을 수 없습니다.")
    if source == state or (state.exists() and source.samefile(state)):
        raise ValueError("공고 DB와 개인 상태 DB는 다른 파일이어야 합니다.")
    return source, state


def _connect(path, mode):
    db = sqlite3.connect(Path(path).resolve().as_uri() + f"?mode={mode}", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return db


def init_state_db(jobs_db: Path, state_db: Path) -> None:
    source, state = _validate_paths(jobs_db, state_db)
    with closing(_connect(source, "ro")) as db:
        db.execute("SELECT id,document,fingerprint,decision,reason,evidence_lines,reviewed_at FROM posts LIMIT 0")
        db.execute("SELECT key,value FROM metadata LIMIT 0")
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(state, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    state.chmod(0o600)
    with closing(_connect(state, "rw")) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS job_state (
            post_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'unmarked'
                CHECK(status IN ('unmarked','interested','preparing','applied','archived')),
            seen_reviewed_at TEXT,
            updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS profile_posts (
            id INTEGER PRIMARY KEY CHECK(id < 0), source_key TEXT NOT NULL UNIQUE,
            document TEXT NOT NULL, confirmed_on TEXT NOT NULL, imported_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS application_stages (
            post_id INTEGER NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ('document','interview1','interview2','final')),
            result TEXT NOT NULL CHECK(result IN ('unknown','not_started','in_progress','passed','failed','skipped')),
            event_date TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
            PRIMARY KEY(post_id,stage)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS job_availability (
            post_id INTEGER PRIMARY KEY CHECK(post_id > 0),
            closed INTEGER NOT NULL CHECK(closed IN (0,1)), deadline_at TEXT,
            evidence TEXT NOT NULL, checked_at TEXT NOT NULL,
            source_modified_at TEXT, source_url TEXT NOT NULL
        )""")


def _json(value, default):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _job(row, state, cached_availability=None):
    document = _json(row["document"], None)
    error = not isinstance(document, dict)
    doc = document if isinstance(document, dict) else {}
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        title = f"공고 #{row['id']}"
    company, separator, role = title.partition("｜")
    lines = doc.get("lines", [])
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        lines, error = [], True
    evidence = _json(row["evidence_lines"], [])
    if not isinstance(evidence, list):
        evidence = []
    evidence = [n for n in evidence if type(n) is int and 1 <= n <= len(lines)]
    images = doc.get("images", [])
    if not isinstance(images, list):
        images = []
    reviewed = timestamp(row["reviewed_at"])
    seen = timestamp(state.get("seen_reviewed_at"))
    return {
        **availability(doc if row["id"] > 0 else {}, cached_availability),
        "id": row["id"], "title": title,
        "company": company.strip() if separator else "기업명 미기재",
        "role": role.strip() if separator else title,
        "reason": row["reason"] or "", "decision": row["decision"],
        "reviewed_at": reviewed, "published_at": timestamp(doc.get("published_gmt")),
        "fingerprint": row["fingerprint"], "source_url": safe_url(doc.get("url")),
        "apply_url": safe_url(doc.get("apply_url")), "lines": lines,
        "evidence_lines": evidence,
        "images": [{"url": safe_url(item.get("url")), "alt": item.get("alt", "")}
                   for item in images if isinstance(item, dict) and safe_url(item.get("url"))],
        "status": state.get("status", "unmarked"),
        "source_kind": "inthiswork", "source_label": "INTHISWORK", "recorded_at": None,
        "state_updated_at": timestamp(state.get("updated_at")),
        "is_unread": bool(reviewed and (not seen or reviewed > seen)),
        "display_error": "저장된 본문 일부를 표시할 수 없습니다. 원문을 확인해 주세요." if error else "",
    }


def _profile_job(row, state):
    doc = _json(row["document"], {})
    note = doc.get("record_note") if isinstance(doc, dict) else None
    reason = note.strip() if isinstance(note, str) and note.strip() else (
        f"사용자가 지원 완료를 확인한 기록입니다. (확인일 {row['confirmed_on']}, 실제 지원일 미기록)")
    item = _job({"id": row["id"], "document": row["document"], "fingerprint": "",
                 "reason": reason,
                 "decision": None, "evidence_lines": "[]", "reviewed_at": None}, state)
    item.update(source_kind="profile", source_label="personal-profile", recorded_at=timestamp(row["imported_at"]))
    return item


def _with_progress(item, stages):
    item["progress"] = {key: {"result": "unknown", "event_date": "", "note": "", "updated_at": None}
                        for key in STAGE_LABELS}
    for row in stages:
        if row["stage"] in STAGE_LABELS:
            item["progress"][row["stage"]] = {key: row[key] for key in ("result", "event_date", "note", "updated_at")}
    known = [(key, value) for key, value in item["progress"].items()
             if value["result"] not in {"unknown", "not_started", "skipped"}]
    item["progress_label"] = f"{STAGE_LABELS[known[-1][0]]} · {RESULT_LABELS[known[-1][1]['result']]}" if known else "결과 미확인"
    if all(value["result"] == "skipped" for value in item["progress"].values()):
        item["progress_label"] = "채용 전형 해당 없음"
    item["activity_at"] = max(filter(None, [item["state_updated_at"], item["recorded_at"], item["reviewed_at"],
        *[timestamp(value["updated_at"]) for value in item["progress"].values()]]), default="")
    return item


def _matches(item, view):
    if view == "expired":
        return (item["is_expired"] and item["status"] not in {"applied", "archived"}
                and (item["decision"] == "recommend" or item["status"] in {"interested", "preparing"}))
    if view in {"recommended", "needs_review"}:
        decision = "recommend" if view == "recommended" else "needs_review"
        return (item["decision"] == decision and item["status"] not in {"applied", "archived"}
                and (view != "recommended" or not item["is_expired"]))
    return item["status"] == view


def list_jobs(jobs_db: Path, state_db: Path, *, view="recommended", q="", page=1) -> dict:
    if view not in VIEWS or not isinstance(q, str) or len(q) > 200 or type(page) is not int or page < 1:
        raise ValueError("보기, 검색어 또는 페이지를 확인해 주세요.")
    source, state = _validate_paths(jobs_db, state_db)
    with closing(_connect(source, "ro")) as db:
        rows = db.execute("SELECT * FROM posts").fetchall()
        checkpoint = db.execute("SELECT value FROM metadata WHERE key='last_success'").fetchone()
    with closing(_connect(state, "ro")) as db:
        states = {r["post_id"]: dict(r) for r in db.execute("SELECT * FROM job_state")}
        profiles = db.execute("SELECT * FROM profile_posts").fetchall()
        availability_rows = {r["post_id"]: dict(r) for r in db.execute("SELECT * FROM job_availability")}
        stages = {}
        for row in db.execute("SELECT * FROM application_stages"):
            stages.setdefault(row["post_id"], []).append(row)
    # The current personal corpus is small (~1,000); parse once, never hold a source lock while rendering.
    items = [_job(row, states.get(row["id"], {}), availability_rows.get(row["id"])) for row in rows]
    items += [_profile_job(row, states.get(row["id"], {})) for row in profiles]
    items = [_with_progress(item, stages.get(item["id"], [])) for item in items]
    counts = {v: sum(_matches(item, v) for item in items) for v in VIEWS}
    needle = q.strip().casefold()
    filtered = [item for item in items if _matches(item, view)
                and (not needle or needle in (item["title"] + " " + item["reason"]).casefold())]
    filtered.sort(key=lambda item: ((item["activity_at"] if view == "applied" else None)
                  or item["reviewed_at"] or item["published_at"] or item["recorded_at"] or "", item["id"]), reverse=True)
    total = len(filtered)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    result = [{k: v for k, v in item.items() if k not in {"lines", "images", "progress"}}
              for item in filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
    return {"items": result, "total": total, "page": page, "total_pages": total_pages,
            "counts": counts, "last_collected_at": timestamp(checkpoint[0]) if checkpoint else None}


def get_job(jobs_db: Path, state_db: Path, post_id: int):
    source, state = _validate_paths(jobs_db, state_db)
    with closing(_connect(source, "ro")) as db:
        row = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    with closing(_connect(state, "ro")) as db:
        personal = db.execute("SELECT * FROM job_state WHERE post_id=?", (post_id,)).fetchone()
        profile = db.execute("SELECT * FROM profile_posts WHERE id=?", (post_id,)).fetchone() if post_id < 0 else None
        stages = db.execute("SELECT * FROM application_stages WHERE post_id=?", (post_id,)).fetchall()
        cached = db.execute("SELECT * FROM job_availability WHERE post_id=?", (post_id,)).fetchone()
    if row is None and profile is None:
        return None
    personal = dict(personal) if personal else {}
    item = _profile_job(profile, personal) if profile is not None else _job(row, personal, dict(cached) if cached else None)
    return _with_progress(item, stages)


def _date(value):
    if not isinstance(value, str):
        raise ValueError("날짜는 YYYY-MM-DD 형식으로 입력해 주세요.")
    if value and date.fromisoformat(value).isoformat() != value:
        raise ValueError("날짜는 YYYY-MM-DD 형식으로 입력해 주세요.")
    return value


def set_stage(jobs_db, state_db, post_id, stage, result, event_date, note):
    if not isinstance(stage, str) or stage not in STAGE_LABELS or not isinstance(result, str) or result not in RESULT_LABELS:
        raise ValueError("전형 단계와 결과를 확인해 주세요.")
    if not isinstance(note, str) or len(note) > 1000:
        raise ValueError("메모는 1000자 이내로 입력해 주세요.")
    _date(event_date)
    job = get_job(jobs_db, state_db, post_id)
    if job is None:
        raise LookupError("공고를 찾을 수 없습니다.")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(_connect(state_db, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute("SELECT status FROM job_state WHERE post_id=?", (post_id,)).fetchone()
        if state is None or state[0] != "applied":
            raise ValueError("지원 완료로 표시한 뒤 전형 결과를 기록해 주세요.")
        db.execute("""INSERT INTO application_stages VALUES(?,?,?,?,?,?)
            ON CONFLICT(post_id,stage) DO UPDATE SET result=excluded.result,event_date=excluded.event_date,
            note=excluded.note,updated_at=excluded.updated_at""", (post_id, stage, result, event_date, note, now))
        stages = db.execute("SELECT * FROM application_stages WHERE post_id=?", (post_id,)).fetchall()
    return {"stage": stage, "result": result, "event_date": event_date, "note": note, "updated_at": now,
            "progress_label": _with_progress(job, stages)["progress_label"]}


def import_profile_jobs(jobs_db, state_db, records):
    _validate_paths(jobs_db, state_db)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    created = skipped = 0
    with closing(_connect(state_db, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for record in records:
            for field, limit in (("source_key", 1000), ("company", 300), ("role", 500), ("summary", 20000)):
                if not isinstance(record.get(field), str) or not record[field].strip() or len(record[field]) > limit:
                    raise ValueError(f"프로필 {field} 값을 확인해 주세요.")
            if not record["source_key"].startswith("applications/") or not _date(record.get("confirmed_on")):
                raise ValueError("프로필 출처와 확인일이 필요합니다.")
            source_url = record.get("source_url")
            if source_url and not safe_url(source_url):
                raise ValueError("프로필 출처 URL을 확인해 주세요.")
            document_result = record.get("document_result", "unknown")
            if document_result not in {"unknown", "passed", "failed"}:
                raise ValueError("기존 서류 결과를 확인해 주세요.")
            if db.execute("SELECT id FROM profile_posts WHERE source_key=?", (record["source_key"],)).fetchone():
                skipped += 1
                continue
            post_id = min(0, db.execute("SELECT coalesce(min(id),0) FROM profile_posts").fetchone()[0]) - 1
            doc = {"title": record["company"] + "｜" + record["role"], "url": source_url,
                   "lines": ["이전 personal-profile 자료 (당시 기록)", *record["summary"].splitlines()], "images": []}
            db.execute("INSERT INTO profile_posts VALUES(?,?,?,?,?)", (post_id, record["source_key"],
                       json.dumps(doc, ensure_ascii=False), record["confirmed_on"], now))
            db.execute("INSERT INTO job_state(post_id,status,updated_at) VALUES(?,'applied',?)", (post_id, now))
            if document_result != "unknown":
                db.execute("INSERT INTO application_stages VALUES(?,'document',?,'',?,?)",
                           (post_id, document_result, "personal-profile의 이전 기록에서 확인. 최신 진행 여부는 직접 갱신해 주세요.", now))
            created += 1
    return {"created": created, "skipped": skipped}


def set_status(jobs_db: Path, state_db: Path, post_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError("알 수 없는 지원 상태입니다.")
    job = get_job(jobs_db, state_db, post_id)
    if job is None:
        raise LookupError("공고를 찾을 수 없습니다.")
    if job["source_kind"] == "profile" and status == "unmarked":
        raise ValueError("기존 지원 기록은 표시를 해제할 수 없어요. 보관을 이용해 주세요.")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(_connect(state_db, "rw")) as db, db:
        db.execute("""INSERT INTO job_state(post_id,status,updated_at) VALUES(?,?,?)
            ON CONFLICT(post_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at""",
                   (post_id, status, now))
    return {"status": status}


def _write_seen(state_db, post_id, reviewed_at):
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(_connect(state_db, "rw")) as db, db:
        db.execute("""INSERT INTO job_state(post_id,seen_reviewed_at,updated_at) VALUES(?,?,?)
            ON CONFLICT(post_id) DO UPDATE SET
                seen_reviewed_at=excluded.seen_reviewed_at,updated_at=excluded.updated_at
            WHERE job_state.seen_reviewed_at IS NULL OR job_state.seen_reviewed_at < excluded.seen_reviewed_at""",
                   (post_id, reviewed_at, now))


def mark_seen(jobs_db: Path, state_db: Path, post_id: int, reviewed_at: str) -> bool:
    requested = timestamp(reviewed_at)
    if requested is None:
        raise ValueError("올바른 검토 시각이 필요합니다.")
    job = get_job(jobs_db, state_db, post_id)
    if job is None:
        raise LookupError("공고를 찾을 수 없습니다.")
    if job["reviewed_at"] != requested:
        return False
    _write_seen(state_db, post_id, requested)
    return True
