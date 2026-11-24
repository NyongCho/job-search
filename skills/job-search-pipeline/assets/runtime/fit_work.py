"""Coordinate one durable evaluation per JD, applicant evidence and scoring contract."""

import argparse
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import fit_hermes
from fit_digest import checked_link, dashboard_origin, review_decision, review_digest
from job_availability import _stamp, availability, deadline_view, source_document
from job_fit import evaluate_fit, jd_input, profile_jd, read_fit, recommendation_group, stable_hash, validate_entry

DEFAULT_BATCH_SIZE = 3
RUBRIC = stable_hash([2, fit_hermes.PROMPT])
COUNTERS = ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS fit_items(post_id INTEGER,input_key TEXT,attempt_id TEXT,
        status TEXT NOT NULL,entry TEXT,fit TEXT,legacy INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(post_id,input_key))""",
    """CREATE TABLE IF NOT EXISTS fit_attempts(attempt_id TEXT PRIMARY KEY,started REAL NOT NULL,
        elapsed_seconds REAL,submitted INTEGER NOT NULL,successful INTEGER,failed INTEGER,metrics TEXT)""",
    """CREATE TABLE IF NOT EXISTS fit_watches(work_id TEXT,attempt_id TEXT,owner INTEGER NOT NULL,
        PRIMARY KEY(work_id,attempt_id))""",
    """CREATE TABLE IF NOT EXISTS fit_attempt_origins(attempt_id TEXT PRIMARY KEY REFERENCES fit_attempts(attempt_id),
        run_id INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fit_current(post_id INTEGER PRIMARY KEY,input_key TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fit_delivery(post_id INTEGER PRIMARY KEY,execution_id TEXT,attempted_at REAL)""",
)


def _career_paragraphs(text, relevant):
    for memory in re.split(r"(?m)^§[ \t]*$", text):
        career_depth = None
        for section in re.split(r"(?m)(?=^#{1,6}[ \t])", memory):
            heading = re.match(r"(#{1,6})[ \t]+([^\n]+)", section)
            if heading and (career_depth is None or len(heading[1]) <= career_depth):
                career_depth = len(heading[1]) if relevant.search(heading[2]) else None
            for paragraph in re.split(r"\n\s*\n", section):
                if career_depth is not None or relevant.search(paragraph):
                    yield paragraph


def read_profile():
    """Keep line-addressable facts from career material explicitly connected by this user."""
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    files = [home / "memories" / name for name in ("USER.md", "MEMORY.md")]
    extra = os.environ.get("FIT_PROFILE_FILES", "")
    linked = {Path(name).expanduser().resolve() for name in extra.split(os.pathsep) if name}
    relevant = re.compile(
        r"경력|직무|경험|채용|회사|프로젝트|기술|선호|근무|직장|학력|영어|취업|이력서|답변|지원\s*자격|"
        r"career|engineer|work|job|resume|experience|prefer|skill|role|remote|employment|qualification|career answer",
        re.I,
    )
    facts = []
    for file in files:
        if not file.exists():
            continue
        for paragraph in _career_paragraphs(file.read_text(), relevant):
            for line in paragraph.splitlines():
                line = line.strip()
                if re.search(r"경력|이력|프로필|profile|cv|résumé|resume", line, re.I):
                    paths = set(re.findall(r"(?<![\w/])(?:~|/)[^\s`<>\])]+?\.md\b", line))
                    paths.update(re.findall(r"\[[^\]]*\]\(<?([^)>]+?\.md)>?\)", line, re.I))
                    for name in paths:
                        if not urlsplit(name).scheme and not name.startswith("//"):
                            linked.add((file.parent / Path(unquote(name)).expanduser()).resolve())
                if line:
                    facts.append(line)
    for file in sorted(linked):
        # A missing explicitly linked career document must not turn known experience into an empty profile.
        for line in file.read_text().splitlines():
            line = line.strip()
            if line:
                facts.append(line)
    # Preserve the source's heading and neighboring evidence order in the model payload.
    return {f"P{i:04d}": line for i, line in enumerate(facts, 1)}


def _db(path, mode="rw"):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=" + mode, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _exists(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _answer_facts(state, post_id, jd_version):
    if post_id <= 0 or not _exists(state, "review_answers"):
        return {}, None
    rows = state.execute("""SELECT question,answer,run_id FROM review_answers
        WHERE post_id=? AND jd_version=? ORDER BY question""", (post_id, jd_version)).fetchall()
    facts = {f"A{i:04d}": f"{row[0]}\n{row[1]}" for i, row in enumerate(rows, 1)}
    return facts, max((row[2] for row in rows), default=None)


def _bind_poster_answers(state, doc, version):
    """Bind an answer about the same source to its first readable transcription."""
    if (doc.get("poster_evidence") or {}).get("status") != "ready" or not _exists(state, "review_answers"):
        return
    source_version = stable_hash(jd_input(source_document(doc)))
    if source_version != version:
        # Move once. A later change to the poster's requirements must not inherit that answer again.
        state.execute("""UPDATE OR IGNORE review_answers SET jd_version=? WHERE post_id=? AND jd_version=?""",
                      (version, doc["id"], source_version))


def _key(doc, profile, answers=None):
    parts = [RUBRIC, stable_hash(jd_input(doc)), stable_hash(profile)]
    if answers:
        parts.append(stable_hash(answers))
    return stable_hash(parts)


def _versioned_key(db, doc, profile, answers):
    base = _key(doc, profile, answers)
    if doc["id"] <= 0:
        return base
    row = db.execute("SELECT value FROM metadata WHERE key=?", (f"manual_review:{doc['id']}:{base}",)).fetchone()
    return f"{base}:{row[0]}" if row else base


def cache_manual_review(db, post_id, entry, fit):
    """Supersede this job's active input while keeping earlier attempts and receipts recoverable."""
    current = db.execute("SELECT input_key FROM fit_current WHERE post_id=?", (post_id,)).fetchone()
    if current:
        base = current[0].partition(":")[0]
    else:
        row = db.execute("SELECT document FROM posts WHERE id=?", (post_id,)).fetchone()
        base = _key(json.loads(row[0]), read_profile())
    revision = uuid.uuid4().hex
    db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (f"manual_review:{post_id}:{base}", revision))
    key = f"{base}:{revision}"
    db.execute("INSERT OR REPLACE INTO fit_current VALUES(?,?)", (post_id, key))
    if fit:
        # A human-verified opening has the same fallback semantics as an imported assessment.
        cached = {**fit, "legacy_input_unknown": True}
        saved = {k: entry[k] for k in ("id", "decision", "reason", "required_summary") if k in entry}
        saved["evidence_lines"] = entry.get("evidence_lines", [])
        db.execute("""INSERT INTO fit_items(post_id,input_key,status,entry,fit,legacy)
            VALUES(?,?,'done',?,?,1)""", (post_id, key, json.dumps(saved), json.dumps(cached)))


def migrate(jobs_path, state_path, *, profile=None):
    from collect_jobs import migrate_fit

    migrate_fit(jobs_path)
    profile = read_profile() if profile is None else profile
    with closing(_db(jobs_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            db.execute(statement)
        if "attempted_at" not in {r[1] for r in db.execute("PRAGMA table_info(fit_delivery)")}:
            db.execute("ALTER TABLE fit_delivery ADD COLUMN attempted_at REAL")
        # Keep delivery receipts when a rolled-back release writes only to posts.
        db.execute("""CREATE TRIGGER IF NOT EXISTS fit_delivery_for_new_post AFTER INSERT ON posts
            BEGIN INSERT INTO fit_delivery(post_id) VALUES(NEW.id); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS fit_delivery_for_legacy_review AFTER UPDATE OF reviewed_at ON posts
            WHEN NEW.decision IS NOT NULL AND NEW.reviewed_at IS NOT NULL
                AND NEW.reviewed_at IS NOT OLD.reviewed_at AND NEW.fit_review IS OLD.fit_review
            BEGIN UPDATE fit_delivery SET execution_id='legacy' WHERE post_id=NEW.id AND execution_id IS NULL; END""")
        if not db.execute("SELECT 1 FROM metadata WHERE key='fit_s0'").fetchone():
            db.execute("INSERT INTO metadata VALUES('fit_s0',?)", (stable_hash(profile),))
            for row in db.execute("SELECT * FROM posts"):
                db.execute(
                    """INSERT INTO fit_delivery(post_id,execution_id) SELECT ?,
                    CASE WHEN ? OR EXISTS (SELECT 1 FROM metadata WHERE key=?) THEN 'legacy' END""",
                    (row["id"], bool(row["decision"] or row["reviewed_at"]), "poster_legacy_receipt:" + str(row["id"])),
                )
                fit = read_fit(row)
                if fit:
                    doc = json.loads(row["document"])
                    fit["legacy_input_unknown"] = True
                    try:
                        lines = json.loads(row["evidence_lines"] or "[]")
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(lines, list) or any(
                        type(n) is not int or not 1 <= n <= len(doc["lines"]) for n in lines
                    ):
                        continue
                    entry = {
                        "id": row["id"],
                        "reason": row["reason"],
                        "decision": row["decision"],
                        "evidence_lines": lines,
                    }
                    db.execute("INSERT INTO fit_current VALUES(?,?)", (row["id"], _key(doc, profile)))
                    db.execute(
                        """INSERT INTO fit_items(post_id,input_key,status,entry,fit,legacy)
                        VALUES(?,?,'done',?,?,1)""",
                        (row["id"], _key(doc, profile), json.dumps(entry), json.dumps(fit)),
                    )
    if state_path:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not Path(state_path).exists():
            Path(state_path).touch(mode=0o600, exist_ok=True)
        with closing(_db(state_path)) as state, state:
            for statement in SCHEMA[:-1]:
                state.execute(statement)
            state.execute("""CREATE TABLE IF NOT EXISTS profile_fits(post_id INTEGER PRIMARY KEY,
                input_key TEXT NOT NULL, fit TEXT NOT NULL)""")


def profile_fit(state, post_id):
    if _exists(state, "profile_fits"):
        row = state.execute(
            """SELECT f.fit,f.input_key,p.document FROM profile_fits f
            JOIN profile_posts p ON p.id=f.post_id WHERE f.post_id=?""",
            (post_id,),
        ).fetchone()
        if row and row[1] == stable_hash(profile_jd(json.loads(row[2]), post_id)):
            return json.loads(row[0])
    return None


def usage(jobs_path, work_ids, state_path=None):
    if state_path:
        return usage(jobs_path, work_ids) + usage(state_path, work_ids)

    if not work_ids or not Path(jobs_path).exists():
        return []
    with closing(_db(jobs_path, "ro")) as db:
        if not _exists(db, "fit_watches"):
            return []
        origin = ("(SELECT run_id FROM fit_attempt_origins WHERE attempt_id=a.attempt_id)"
                  if _exists(db, "fit_attempt_origins") else "NULL")
        rows = db.execute(
            f"""SELECT a.*,max(w.owner) AS owned,{origin} AS owner_run_id,
            (SELECT min(CAST(o.work_id AS INTEGER)) FROM fit_watches o WHERE o.owner=1
             AND o.attempt_id=a.attempt_id AND o.work_id NOT GLOB '*[^0-9]*') AS owner_work_id
            FROM fit_watches w
            JOIN fit_attempts a USING(attempt_id) WHERE w.work_id IN (SELECT value FROM json_each(?))
            GROUP BY a.attempt_id ORDER BY a.started""",
            (json.dumps([str(i) for i in work_ids]),),
        )
        result = []
        for row in rows:
            data = dict(row)
            measured = json.loads(data.pop("metrics") or "{}")
            result.append({**data, **{k: measured.get(k) for k in COUNTERS}, "owned": bool(data["owned"])})
        return result


def _timing(state, doc):
    cached, override = None, None
    if state and _exists(state, "job_availability"):
        row = state.execute("SELECT * FROM job_availability WHERE post_id=?", (doc["id"],)).fetchone()
        if row:
            cached = dict(row)
            if _exists(state, "job_deadline_facts"):
                row = state.execute(
                    "SELECT * FROM job_deadline_facts WHERE post_id=? AND checked_at=?",
                    (doc["id"], cached["checked_at"]),
                ).fetchone()
                if row:
                    cached.update(dict(row))
    if state and _exists(state, "job_deadline_overrides"):
        row = state.execute("SELECT * FROM job_deadline_overrides WHERE post_id=?", (doc["id"],)).fetchone()
        if row:
            override = dict(row)
    return availability(doc, cached, override=override)


def _facts(state, doc):
    view = _timing(state, doc)
    apply = doc.get("apply_url")
    try:
        apply = checked_link(apply)
    except ValueError:
        apply = None
    if view["is_expired"]:
        status = "closed"
    elif apply and view["deadline_kind"] != "unknown":
        status = "open"
    else:
        status = "unknown"
    return status, apply, view["deadline_label"]


def _post(db, post_id):
    if post_id > 0:
        return db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    row = db.execute("SELECT document FROM profile_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        return None
    doc = profile_jd(json.loads(row["document"]), post_id)
    return dict(document=json.dumps(doc), id=post_id, fingerprint=stable_hash(doc), reviewed_at=None)


def _publish(db, state, row, entry, fit, verified=None, *, use_previous_check=True):
    doc = json.loads(row["document"])
    if row["id"] < 0:
        db.execute(
            "INSERT OR REPLACE INTO profile_fits VALUES(?,?,?)",
            (row["id"], row["fingerprint"], json.dumps(fit, ensure_ascii=False)),
        )
        return
    status, apply, deadline = _facts(state, doc)
    if status == "unknown" and (verified or use_previous_check):
        check = None
        if verified:
            status, apply, deadline = verified
            check = dict(
                status=status,
                apply_url=apply,
                deadline=deadline,
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
        elif previous := read_fit(row):
            check = previous.get("opening_check")
        if check:
            fit["opening_check"] = check
            status, apply, deadline = check["status"], check["apply_url"], check["deadline"]
    if fit.get("legacy_input_unknown") and status == "unknown":
        status = fit.get("availability", "unknown")
        apply = fit.get("apply_url") or apply
    fit.update(
        fingerprint=row["fingerprint"],
        reviewed_at=row["reviewed_at"] or datetime.now(timezone.utc).isoformat(),
        availability=status,
        assessment_decision=entry["decision"],
    )
    if apply:
        fit["apply_url"] = checked_link(apply)
    else:
        fit.pop("apply_url", None)
    decision = review_decision(entry["decision"], fit)
    db.execute(
        """UPDATE posts SET decision=?,reason=?,evidence_lines=?,reviewed_at=?,fit_review=? WHERE id=?""",
        (
            decision,
            entry["reason"],
            json.dumps(entry["evidence_lines"]),
            fit["reviewed_at"],
            json.dumps(fit, ensure_ascii=False),
            row["id"],
        ),
    )
    db.execute("DELETE FROM metadata WHERE key=?", ("poster_fit:" + str(row["id"]),))
    return doc, {**entry, "deadline": deadline}, decision, fit


@contextmanager
def _slot(directory, count):
    # The child inherits the slot fd. After a parent crash, recovery waits for its durable output.
    while True:
        for i in range(count):
            handle = (directory / f"slot-{i}").open("a+")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
            else:
                try:
                    yield handle.fileno()
                finally:
                    handle.close()
                return
        time.sleep(0.1)


def _batch(path, state_path, tasks, profile, fixed_profile, runner, slots, directory, finish):
    from poster_review import hold_reason

    attempt = uuid.uuid4().hex
    maximum = int(os.environ.get("FIT_INPUT_CHAR_LIMIT", "60000"))
    with (
        ExitStack() as handles,
        closing(_db(path)) as db,
        closing(_db(state_path)) as state,
    ):
        todo, cached, recovering, aliases = [], [], {}, {}
        docs, overflow = [], []
        with state, db:
            db.execute("BEGIN IMMEDIATE")
            if tasks[0]["post_id"] > 0:
                # Serialize the input snapshot and cache publication with concurrent form submissions.
                state.execute("BEGIN IMMEDIATE")
            if not fixed_profile:
                profile = read_profile()
            for task in tasks:
                row = _post(db, task["post_id"])
                if not row:
                    cached.append((task, "skipped", "공고가 존재하지 않습니다."))
                    continue
                doc = json.loads(row["document"])
                if row["id"] > 0 and (reason := hold_reason(doc)):
                    cached.append((task, "skipped", reason))
                    continue
                jd = jd_input(doc)
                version = stable_hash(jd)
                _bind_poster_answers(state, doc, version)
                facts, revision = _answer_facts(state, row["id"], version)
                if task.get("version", "").startswith("answer:") and task["version"] != "answer:" + version:
                    # The form's original group can finish only if its receipt now belongs to this JD.
                    if not facts or not state.execute("""SELECT 1 FROM review_answers
                            WHERE post_id=? AND jd_version=? AND run_id=?""",
                            (row["id"], version, task.get("run_id"))).fetchone():
                        cached.append((task, "skipped", "답변 이후 공고 내용이 변경됐어요."))
                        continue
                key = _versioned_key(db, doc, profile, facts)
                current = db.execute(
                    "SELECT input_key FROM fit_current WHERE post_id=?", (row["id"],)
                ).fetchone()
                db.execute("INSERT OR REPLACE INTO fit_current VALUES(?,?)", (row["id"], key))
                if current and current[0] != key:
                    if row["id"] > 0:
                        db.execute(
                            """UPDATE posts SET fit_review=NULL,decision=NULL,reason=NULL,evidence_lines=NULL
                            WHERE id=?""",
                            (row["id"],),
                        )
                    else:
                        db.execute("DELETE FROM profile_fits WHERE post_id=?", (row["id"],))
                saved = db.execute(
                    "SELECT * FROM fit_items WHERE post_id=? AND input_key=?", (row["id"], key)
                ).fetchone()
                if saved and saved["status"] == "done":
                    previous = json.loads(saved["fit"])
                    if revision is not None:
                        previous["answer_revision"] = revision
                    _publish(db, state, row, json.loads(saved["entry"]), previous)
                    cached.append((task, "done", "기존 평가"))
                    continue
                if row["id"] < 0 and not doc["lines"]:
                    fit = evaluate_fit(
                        {
                            "version": 1,
                            "complete": False,
                            "eligibility": "unknown",
                            "preference": "unknown",
                            "criteria": [],
                        },
                        line_count=0,
                    )
                    db.execute(
                        """INSERT INTO fit_items(post_id,input_key,status,entry,fit)
                        VALUES(?,?,'done','{}',?)""",
                        (row["id"], key, json.dumps(fit)),
                    )
                    _publish(db, state, row, {}, fit)
                    cached.append((task, "done", "공고 원문이 없어 평가를 보류했습니다."))
                    continue
                item = (
                    task,
                    key,
                    row["fingerprint"],
                    dict(id=row["id"], version=version, **jd,
                         **({"answer_facts": facts, "answer_revision": revision} if facts else {})),
                )
                if saved and saved["status"] in {"running", "uncertain"}:
                    prev = saved["attempt_id"]
                    db.execute("INSERT OR IGNORE INTO fit_watches VALUES(?,?,0)", (str(task["id"]), prev))
                    if prev == attempt:
                        aliases.setdefault(row["id"], []).append(task)
                    else:
                        recovering.setdefault(prev, []).append(item)
                    continue
                # Measure the exact numbered prompt using the JD and profile locked in for this attempt.
                # A single oversized JD still runs alone with every source line intact.
                if overflow or (docs and (facts or "answer_facts" in docs[0]
                                         or len(fit_hermes.prompt(profile, [*docs, item[-1]])) > maximum)):
                    overflow.append(task)
                    continue
                docs.append(item[-1])
                todo.append(item)
                db.execute(
                    """INSERT INTO fit_items(post_id,input_key,status,attempt_id) VALUES(?,?,'running',?)
                    ON CONFLICT(post_id,input_key) DO UPDATE SET status='running',attempt_id=excluded.attempt_id""",
                    (row["id"], key, attempt),
                )
                db.execute("INSERT INTO fit_watches VALUES(?,?,1)", (str(task["id"]), attempt))
            if todo:
                guard = handles.enter_context((directory / (attempt + ".lock")).open("a+"))
                fcntl.flock(guard, fcntl.LOCK_EX)
                db.execute(
                    "INSERT INTO fit_attempts(attempt_id,started,submitted) VALUES(?,?,?)",
                    (attempt, time.time(), len(todo)),
                )
                # Shared batches are charged once, to the request that supplied their first new evaluation.
                owner_run = todo[0][0].get("run_id")
                if owner_run is not None:
                    db.execute("INSERT INTO fit_attempt_origins VALUES(?,?)", (attempt, owner_run))
        for item in cached:
            finish(*item)
        unsubmitted = []
        for prior, items in recovering.items():
            # A different slot can still be running this exact attempt.
            with (directory / (prior + ".lock")).open("a+") as prior_guard:
                fcntl.flock(prior_guard, fcntl.LOCK_EX)
                result = fit_hermes.recover(directory, prior)
                if result is None and not (directory / (prior + ".submitted")).exists():
                    # Both parent and child have released this guard. The adapter fsyncs the marker
                    # before entering Hermes, so this attempt can no longer reach the provider.
                    with db:
                        db.execute(
                            """UPDATE fit_items SET status='failed' WHERE attempt_id=?
                            AND status IN ('running','uncertain')""",
                            (prior,),
                        )
                        db.execute(
                            """UPDATE fit_attempts SET submitted=0,successful=0,failed=0,metrics=?,
                            elapsed_seconds=coalesce(elapsed_seconds,?-started) WHERE attempt_id=?""",
                            (json.dumps(dict.fromkeys(COUNTERS, 0)), time.time(), prior),
                        )
                    unsubmitted.extend(item[0] for item in items)
                else:
                    _complete(db, state, prior, items, profile, fixed_profile, result, finish)
        if not todo:
            return unsubmitted
        with _slot(directory, slots) as fd:
            try:
                if runner:
                    result = runner(attempt, profile, docs)
                    if "answer_facts" in docs[0] and result is not None:
                        result = fit_hermes.private_receipt(result, profile, docs[0])
                    fit_hermes.atomic_json(directory / (attempt + ".json"), result)
                else:
                    fit_hermes.run(attempt, profile, docs, directory, (fd, guard.fileno()))
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                if runner is None and not (directory / (attempt + ".submitted")).exists():
                    fit_hermes.atomic_json(
                        directory / (attempt + ".json"),
                        {**dict.fromkeys(COUNTERS, 0), "submitted": 0, "entries": [], "error": "Hermes 평가 실행 준비 실패"},
                    )
        result = fit_hermes.recover(directory, attempt)

        def publish(task, status, message):
            finish(task, status, message)
            for joined in aliases.get(task["post_id"], []):
                finish(joined, status, message)

        _complete(db, state, attempt, todo, profile, fixed_profile, result, publish)
        return unsubmitted + overflow


def _complete(db, state, attempt, items, profile, fixed_profile, result, finish):
    entries = result.get("entries", []) if isinstance(result, dict) else []
    entries = entries if isinstance(entries, list) else []
    counts = Counter(e["id"] for e in entries if isinstance(e, dict) and type(e.get("id")) is int)
    by_id = {
        e["id"]: e
        for e in entries
        if isinstance(e, dict) and type(e.get("id")) is int and counts[e["id"]] == 1
    }
    for task, key, fingerprint, doc in items:
        post_id = doc["id"]
        outcome, message = "failed", "평가 실행 확인 필요"
        entry, fit = by_id.get(post_id), None
        if result is not None:
            try:
                if not entry:
                    raise ValueError(result.get("error") or "평가 응답에서 공고 누락 또는 중복")
                fit = validate_entry(entry, doc, {**profile, **doc.get("answer_facts", {})})
                if "answer_revision" in doc:
                    fit["answer_revision"] = doc["answer_revision"]
                outcome, message = "done", "평가 완료"
            except (ValueError, KeyError, TypeError, IndexError) as error:
                message = str(error)
        with state, db:
            db.execute("BEGIN IMMEDIATE")
            if post_id > 0:
                state.execute("BEGIN IMMEDIATE")
            row = _post(db, post_id)
            saved = db.execute(
                "SELECT status,attempt_id FROM fit_items WHERE post_id=? AND input_key=?", (post_id, key)
            ).fetchone()
            if saved["attempt_id"] != attempt:
                outcome, message = "skipped", "다른 평가 실행에 연결됨"
            elif fit:
                db.execute(
                    """UPDATE fit_items SET status='done',entry=?,fit=? WHERE post_id=? AND input_key=?""",
                    (
                        json.dumps(entry, ensure_ascii=False),
                        json.dumps(fit, ensure_ascii=False),
                        post_id,
                        key,
                    ),
                )
                latest_profile = profile if fixed_profile else read_profile()
                # Metadata-only replacement can bind the completed assessment to the new fingerprint.
                current_doc = json.loads(row["document"]) if row else {}
                current_jd = row and (
                    row["fingerprint"] == fingerprint or stable_hash(jd_input(current_doc)) == doc["version"]
                )
                active = db.execute(
                    "SELECT input_key FROM fit_current WHERE post_id=?", (post_id,)
                ).fetchone()
                answers, _ = _answer_facts(state, post_id, doc["version"])
                if active and active[0] == key and current_jd and _versioned_key(db, current_doc, latest_profile, answers) == key:
                    _publish(db, state, row, entry, fit)
                else:
                    outcome, message = "skipped", "평가 중 입력이 변경되었습니다."
            elif saved["status"] != "done":
                db.execute(
                    "UPDATE fit_items SET status=? WHERE post_id=? AND input_key=?",
                    ("failed" if result is not None else "uncertain", post_id, key),
                )
            if result is not None and saved["status"] in {"running", "uncertain"}:
                measured = {k: result.get(k) for k in COUNTERS}
                db.execute(
                    """UPDATE fit_attempts SET successful=coalesce(successful,0)+?,
                    failed=coalesce(failed,0)+?,submitted=coalesce(?,submitted),metrics=?,
                    elapsed_seconds=coalesce(elapsed_seconds,?-started) WHERE attempt_id=?""",
                    (int(fit is not None), int(fit is None), result.get("submitted"),
                     json.dumps(measured), time.time(), attempt),
                )
        finish(task, outcome, message)


def refresh_fit_tasks(
    jobs_path, state_path, tasks, finish, *, profile=None, runner=None, batch_size=None, concurrency=None,
    poster_ids=None,
):
    from poster_review import prepare_post

    fixed = profile is not None
    profile = read_profile() if profile is None else profile
    migrate(jobs_path, state_path, profile=profile)
    size = int(os.environ.get("FIT_BATCH_SIZE", DEFAULT_BATCH_SIZE) if batch_size is None else batch_size)
    slots = int(os.environ.get("FIT_CONCURRENCY", "1") if concurrency is None else concurrency)
    if not 1 <= size <= 5 or not 1 <= slots <= 4:
        raise ValueError("FIT_BATCH_SIZE must be 1..5 and FIT_CONCURRENCY must be 1..4")
    directory = Path(str(Path(jobs_path).resolve()) + ".fit-runs")
    directory.mkdir(mode=0o700, exist_ok=True)
    groups = [(path, [task for task in tasks if (task["post_id"] > 0) == positive])
              for positive, path in ((True, jobs_path), (False, state_path))]

    def run_batch(work):
        path, group = work
        while group:
            batch, group = group[:size], group[size:]
            ready = []
            for task in batch:
                reason = ""
                if task["post_id"] > 0 and (poster_ids is None or task["post_id"] in poster_ids):
                    try:
                        reason = prepare_post(jobs_path, state_path, task["post_id"],
                                              check_official=task.get("source_status") != "done")
                    except (OSError, ValueError):
                        finish(task, "failed", "채용 포스터의 원문을 준비하지 못했습니다.")
                        continue
                if reason:
                    finish(task, "skipped", reason)
                else:
                    ready.append(task)
            if ready:
                pending = _batch(path, state_path, ready, profile, fixed, runner, slots, directory, finish)
                group = pending + group

    if slots == 1:
        for group in groups:
            run_batch(group)
    else:
        batches = [(path, group[i:i + size]) for path, group in groups for i in range(0, len(group), size)]
        with ThreadPoolExecutor(slots) as pool:
            list(pool.map(run_batch, batches))


def verify_openings(documents, *, fetch_page=None, fetch=None):
    """Resolve missing routing facts against the same role, without inferring an apply URL."""
    from collect_jobs import JOB_CATEGORIES, application_link, request_json
    from job_sources import fetch_posting, linked_job_locator, source_locator
    from public_fetch import fetch_public
    from posting_checks import checked_inthiswork_posts, include_url, _external_facts

    def verify_official(doc, locator):
        try:
            posting = fetch_posting(locator, fetch or fetch_public, time.monotonic() + 6, previous=doc)
            facts = _external_facts(doc, posting)
            view = deadline_view(facts, datetime.now(timezone.utc))
            if view["is_expired"]:
                return "closed", None, view["label"]
            apply = (application_link(posting.body, "지원하기")
                     or application_link(posting.body, "지원하러가기") or doc.get("apply_url"))
            return "open", checked_link(apply), view["label"]
        except (OSError, ValueError, TypeError, KeyError):
            return None

    groups = {}
    result = {}
    for doc in documents:
        locator = source_locator(doc)
        if not locator:
            continue
        if locator.source == "inthiswork":
            groups[doc["id"]] = (locator, doc)
        elif verified := verify_official(doc, locator):
            result[doc["id"]] = verified
    if groups:
        try:
            posts, _ = (fetch_page or request_json)(include_url(groups))
            matched = checked_inthiswork_posts(posts, {i: s[0] for i, s in groups.items()}, strict=False)
        except (OSError, ValueError, TypeError, KeyError):
            return result
        for i, doc in matched.items():
            view = availability(doc)
            prior = _stamp(groups[i][1].get("modified_gmt"))
            if prior and _stamp(doc["modified_gmt"]) < prior:
                continue
            if view["is_expired"]:
                result[i] = ("closed", None, view["deadline_label"])
            elif (
                doc.get("apply_url")
                and JOB_CATEGORIES.intersection(doc["categories"])
                and (
                    jd_input(doc) == jd_input(source_document(groups[i][1]))
                    or (groups[i][1].get("jd_source") and doc["apply_url"] == groups[i][1].get("apply_url"))
                )
            ):
                if view["deadline_kind"] == "unknown":
                    official = linked_job_locator(doc, doc["apply_url"])
                    if official and (verified := verify_official(groups[i][1], official)):
                        result[i] = verified
                else:
                    try:
                        result[i] = ("open", checked_link(doc["apply_url"]), view["deadline_label"])
                    except ValueError:
                        pass
    return result


def _scheduled_targets(db, state_path, execution_id):
    from poster_backfill import select_posts
    from poster_review import hold_reason

    # A retry (including recovery before delivery) must use the same OCR budget and assessment inputs.
    key = "poster_schedule:" + execution_id
    saved = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    if saved:
        return json.loads(saved[0])
    with closing(_db(state_path, "ro")) as state:
        posters = [item["id"] for item in select_posts(db, state, limit=2, oldest_first=True)]
    targets = posters.copy()
    rows = db.execute("""SELECT post_id,document FROM fit_delivery JOIN posts ON posts.id=post_id
        WHERE execution_id IS NULL OR EXISTS (SELECT 1 FROM metadata WHERE key='poster_fit:' || post_id)
        ORDER BY attempted_at,post_id""")
    for post_id, document in rows:
        if post_id in posters:
            continue
        try:
            doc = json.loads(document)
            if not isinstance(doc, dict) or hold_reason(doc):
                continue
        except (AttributeError, KeyError, TypeError, ValueError):
            # A malformed legacy JD must not block the other postings in this run.
            continue
        targets.append(post_id)
        if len(targets) >= 100:
            break
    db.execute("INSERT INTO metadata VALUES(?,?)", (key, json.dumps([targets, posters])))
    return targets, posters


def scheduled_review(
    jobs_path,
    state_path,
    execution_id,
    *,
    profile=None,
    runner=None,
    dashboard_url=None,
    ids=None,
    verifier=verify_openings,
):
    from poster_review import hold_reason

    if dashboard_url:
        dashboard_url = dashboard_origin(dashboard_url)
    explicit = profile is not None
    profile = read_profile() if profile is None else profile
    migrate(jobs_path, state_path, profile=profile)
    with closing(_db(jobs_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT digest,delivered FROM outbox WHERE id=?", (execution_id,)).fetchone()
        if saved:
            return "[SILENT]" if saved["delivered"] else saved["digest"]
        if db.execute("SELECT 1 FROM metadata WHERE key=?", ("fit_delivery:" + execution_id,)).fetchone():
            return "[SILENT]"
        if ids is None:
            target_ids, poster_ids = _scheduled_targets(db, state_path, execution_id)
        else:
            target_ids, poster_ids = list(dict.fromkeys(ids))[:100], None
        db.execute(
            "UPDATE fit_delivery SET attempted_at=? WHERE post_id IN (SELECT value FROM json_each(?))",
            (time.time(), json.dumps(target_ids)),
        )
    tasks = [dict(id=f"cron:{execution_id}:{post_id}", post_id=post_id, version="") for post_id in target_ids]
    refresh_fit_tasks(
        jobs_path, state_path, tasks, lambda *args: None, profile=profile if explicit else None, runner=runner,
        poster_ids=poster_ids,
    )
    # Network verification runs outside SQLite write locks. The fingerprint is checked again before delivery.
    docs, versions = [], {}
    with closing(_db(jobs_path, "ro")) as view, closing(_db(state_path, "ro")) as state:
        for row in view.execute(
            "SELECT * FROM posts WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(target_ids),)
        ):
            doc = json.loads(row["document"])
            if row["decision"] != "exclude" and _facts(state, doc)[0] == "unknown" and read_fit(row):
                docs.append(doc)
                versions[row["id"]] = row["fingerprint"]
    checked = verifier(docs) if docs else {}
    with closing(_db(jobs_path)) as db, closing(_db(state_path)) as state, state, db:
        db.execute("BEGIN IMMEDIATE")
        state.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT digest,delivered FROM outbox WHERE id=?", (execution_id,)).fetchone()
        if saved:
            return "[SILENT]" if saved["delivered"] else saved["digest"]
        if db.execute("SELECT 1 FROM metadata WHERE key=?", ("fit_delivery:" + execution_id,)).fetchone():
            return "[SILENT]"
        reviews = []
        current = profile if explicit else read_profile()
        inactive = {r[0] for r in state.execute(
            "SELECT post_id FROM job_state WHERE status IN ('applied','archived')"
        )} if _exists(state, "job_state") else set()
        for t in tasks:
            row = db.execute(
                """SELECT p.* FROM posts p JOIN fit_delivery d ON d.post_id=p.id
                WHERE p.id=? AND d.execution_id IS NULL""",
                (t["post_id"],),
            ).fetchone()
            if not row:
                continue
            document = json.loads(row["document"])
            if hold_reason(document):
                continue
            answers, _ = _answer_facts(state, row["id"], stable_hash(jd_input(document)))
            key = _versioned_key(db, document, current, answers)
            saved = db.execute(
                """SELECT entry,fit FROM fit_items JOIN fit_current USING(post_id,input_key)
                    WHERE post_id=? AND input_key=? AND status='done'""",
                (row["id"], key),
            ).fetchone()
            if saved:
                live = checked.get(row["id"]) if versions.get(row["id"]) == row["fingerprint"] else None
                entry = json.loads(saved["entry"])
                review = _publish(
                    db, state, row, entry, json.loads(saved["fit"]), live, use_previous_check=False
                )
                answered_questions = {fact.partition("\n")[0] for fact in answers.values()}
                if row["id"] not in inactive and review[3].get("question") not in answered_questions:
                    reviews.append(review)
                if review[3]["availability"] == "unknown":
                    open_fit = {**review[3], "availability": "open"}
                    if recommendation_group(review_decision(entry["decision"], open_fit), open_fit):
                        continue
                db.execute(
                    "UPDATE fit_delivery SET execution_id=? WHERE post_id=?", (execution_id, row["id"])
                )
        digest = review_digest(reviews, dashboard_url, timing=lambda doc: _timing(state, doc))
        if digest != "[SILENT]":
            db.execute(
                "INSERT INTO outbox(id,digest,attempts) VALUES(?,?,?)",
                (execution_id, digest, json.dumps([execution_id])),
            )
        db.execute("INSERT INTO metadata VALUES(?,?)", ("fit_delivery:" + execution_id, "1"))
        db.execute("DELETE FROM metadata WHERE key=?", ("poster_schedule:" + execution_id,))
        return digest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--state", required=True, type=Path)
    p.add_argument(
        "--schema-only", action="store_true", help="Initialize an isolated validation copy with an empty S0"
    )
    args = p.parse_args()
    migrate(args.db, args.state, profile={} if args.schema_only else None)


if __name__ == "__main__":
    main()
