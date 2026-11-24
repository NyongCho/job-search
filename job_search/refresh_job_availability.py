"""Refresh availability for existing recommendations without rewriting reviews."""
import argparse
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, time as clock, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .card_metadata import has_table
from .collect_jobs import API, FIELDS, extract, request_json
from .company_logos import _plan
from .dashboard_store import init_state_db, read_job_document
from .job_availability import KST, _stamp, availability, document_version, inspect_document
from .job_fit import group_before_closure, read_fit
from .review_routing import question_if_open
from .job_sources import fetch_posting, same_inthiswork_post, source_locator
from .public_fetch import fetch_public

ROOT = Path(__file__).resolve().parents[1]


class SourceCheckFailed(ValueError):
    pass


def _inthiswork(post, expected):
    if (not isinstance(post, dict) or type(post.get("id")) is not int or post["id"] not in expected
            or not isinstance(post.get("categories"), list) or any(type(c) is not int for c in post["categories"])):
        raise ValueError("Invalid Inthiswork availability response")
    for field in ("date_gmt", "modified_gmt"):
        value = post.get(field)
        if not isinstance(value, str) or "T" not in value or not _stamp(value):
            raise ValueError("Invalid Inthiswork source timestamp")
    doc = extract(post)
    if not doc["lines"] and not doc["images"] and not inspect_document(doc)["closed"]:
        raise ValueError("Inthiswork returned no posting content")
    return doc


def include_url(ids):
    return API + "?" + urlencode({"include": ",".join(map(str, ids)), "per_page": 100, "_fields": FIELDS})


def checked_inthiswork_posts(posts, locators, *, strict):
    if not isinstance(posts, list):
        raise ValueError("Expected Inthiswork posts")
    ids = [p.get("id") if isinstance(p, dict) and type(p.get("id")) is int else None for p in posts]
    counts = Counter(ids)
    valid = {}
    for post, post_id in zip(posts, ids):
        try:
            if counts[post_id] != 1:
                raise ValueError("Ambiguous Inthiswork posting")
            doc = _inthiswork(post, locators)
            if same_inthiswork_post(locators[post_id], doc):
                valid[post_id] = doc
        except (ValueError, TypeError, KeyError, AttributeError):
            if strict:
                raise
    return valid


def _external_facts(document, posting):
    facts = inspect_document({"lines": posting.body.lines, "published_gmt": document.get("published_gmt")})
    if posting.due is not None:
        value = posting.due
        if not isinstance(value, str):
            raise ValueError("Invalid source deadline")
        if len(value) == 10:
            at = datetime.combine(date.fromisoformat(value), clock(23, 59, 59), tzinfo=KST)
            kind = "date"
        else:
            at = datetime.fromisoformat(value)
            if not at.tzinfo:
                at = at.replace(tzinfo=KST)
            kind = "datetime"
        facts.update(deadline_at=at.isoformat(), deadline_kind=kind, deadline_word="", evidence=value)
    if posting.closed:
        facts.update(closed=True, evidence="출처에서 접수 종료를 확인함")
    elif not posting.body.lines and not posting.body.images and posting.due is None:
        raise ValueError("Source returned no posting content")
    return facts


def refresh_one(jobs_db, state_db, post_id, *, fetch=fetch_public, now=None):
    if post_id < 0:
        raise ValueError("이전 지원 기록은 자동으로 다시 확인할 수 없어요.")
    started = now or datetime.now(timezone.utc)
    document = read_job_document(jobs_db, post_id)
    locator = source_locator(document)
    if locator is None or (locator.source == "inthiswork" and locator.original_id != str(post_id)):
        raise ValueError("이 공고의 자동 확인 경로를 찾을 수 없어요.")
    deadline = time.monotonic() + 6
    try:
        if locator.source == "inthiswork":
            url = locator.endpoint + "?" + urlencode({"_fields": FIELDS})
            final_url, raw, _ = fetch(url, deadline=deadline, limit=2_000_000)
            if urlsplit(final_url).hostname != "inthiswork.com" or urlsplit(final_url).path != urlsplit(url).path:
                raise ValueError("Inthiswork API redirected away from requested posting")
            fresh = _inthiswork(json.loads(raw), {post_id})
            if not same_inthiswork_post(locator, fresh):
                raise ValueError("Inthiswork posting did not match")
            modified = fresh.get("modified_gmt")
            old_modified = _stamp(document.get("modified_gmt"))
            if old_modified and _stamp(modified) < old_modified:
                raise ValueError("Inthiswork returned an older version")
            facts = inspect_document(fresh)
        else:
            posting = fetch_posting(locator, fetch, deadline, previous=document)
            facts = _external_facts(document, posting)
            modified = posting.modified
    except OSError as error:
        raise SourceCheckFailed("출처에 접속하지 못했어요. 마지막 확인값을 유지했어요.") from error
    except (KeyError, TypeError, ValueError) as error:
        raise SourceCheckFailed("같은 직무의 공개 공고를 확인하지 못했어요. 마지막 확인값을 유지했어요.") from error
    with closing(sqlite3.connect(Path(state_db).resolve().as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if read_job_document(jobs_db, post_id) != document:
            raise SourceCheckFailed("조회 중 저장된 공고가 바뀌었어요. 다시 확인해 주세요.")
        linked = _plan(fresh).locator if locator.source == "inthiswork" and has_table(db, "job_linked_deadlines") else None
        return write_facts(db, post_id, document, facts, started, modified, locator.endpoint, linked=linked)


def accepts_facts(db, post_id, document, started, source_modified):
    version = document_version(document)
    old = db.execute("""SELECT a.checked_at,a.source_modified_at,f.document_version,a.source_url FROM job_availability a
        LEFT JOIN job_deadline_facts f ON a.post_id=f.post_id AND a.checked_at=f.checked_at WHERE a.post_id=?""",
        (post_id,)).fetchone()
    previous = _stamp(old[0]) if old else None
    if previous and started < previous:
        return False
    incoming = _stamp(source_modified)
    stored = _stamp(document.get("modified_gmt"))
    previous_source = _stamp(old[1]) if old and old[2] == version else None
    if old and old[2] == version and document.get("source", "inthiswork") == "inthiswork" and not old[3].startswith(API):
        # A linked official deadline uses that site's modification time in the displayed fact.
        # Earlier writers did not record linked facts or create the companion table.
        if db.execute("PRAGMA table_info(job_linked_deadlines)").fetchone():
            linked = db.execute("""SELECT listing_modified_at FROM job_linked_deadlines
                WHERE post_id=? AND json_extract(locator,'$.url')=?""", (post_id, old[3])).fetchone()
            if linked:
                previous_source = _stamp(linked[0])
    if previous_source and not incoming:
        return False
    if incoming and any(value and incoming < value for value in (stored, previous_source)):
        return False
    return True


def write_facts(db, post_id, document, facts, started, source_modified, source_url, *,
                linked=None, official_facts=None, official_modified=None):
    if not accepts_facts(db, post_id, document, started, source_modified):
        return False
    version = document_version(document)
    checked_at = started.isoformat(timespec="microseconds")
    if linked:
        locator = json.dumps(asdict(linked), sort_keys=True)
        saved = db.execute("""SELECT facts,checked_at,modified_at FROM job_linked_deadlines
            WHERE post_id=? AND locator=?""", (post_id, locator)).fetchone()
        previous = _stamp(saved[2]) if saved else None
        incoming = _stamp(official_modified)
        if official_facts is not None and (not previous or (incoming and incoming > previous)
                or (incoming == previous and official_facts == json.loads(saved[0]))):
            saved = (json.dumps(official_facts, ensure_ascii=False), checked_at, official_modified)
        if saved:
            # Each site has its own revision clock. A listing-only check cannot renew the official success time.
            db.execute("INSERT OR REPLACE INTO job_linked_deadlines VALUES(?,?,?,?,?,?)",
                       (post_id, locator, *saved, source_modified))
            official = json.loads(saved[0])
            if not facts["closed"] and (official["closed"] or official["deadline_kind"] != "unknown"):
                facts, checked_at, source_modified, source_url = official, saved[1], saved[2], linked.url
    db.execute("""INSERT INTO job_availability VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(post_id) DO UPDATE SET closed=excluded.closed,deadline_at=excluded.deadline_at,
        evidence=excluded.evidence,checked_at=excluded.checked_at,source_modified_at=excluded.source_modified_at,
        source_url=excluded.source_url""",
        (post_id, int(facts["closed"]), facts["deadline_at"], facts["evidence"], checked_at, source_modified, source_url))
    db.execute("""INSERT INTO job_deadline_facts VALUES(?,?,?,?,?)
        ON CONFLICT(post_id) DO UPDATE SET checked_at=excluded.checked_at,document_version=excluded.document_version,
        deadline_kind=excluded.deadline_kind,deadline_word=excluded.deadline_word""",
        (post_id, checked_at, version, facts["deadline_kind"], facts["deadline_word"]))
    return True


def refresh(jobs_db, state_db, fetch_page=request_json, now=None):
    now = now or datetime.now(timezone.utc)
    init_state_db(jobs_db, state_db)
    with closing(sqlite3.connect(Path(state_db).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        statuses = dict(db.execute("SELECT post_id,status FROM job_state WHERE post_id>0"))
    with closing(sqlite3.connect(Path(jobs_db).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        stored = {}
        for row in db.execute("SELECT * FROM posts"):
            status = statuses.get(row["id"])
            fit = read_fit(row)
            prior_group = group_before_closure(fit)
            if (row["decision"] == "recommend" or prior_group in {"preferred", "outside"}
                    or status in ("interested", "preparing", "applied")):
                stored[row["id"]] = json.loads(row["document"])
                continue
            decision = (fit.get("assessment_decision", row["decision"]) if fit and
                        fit.get("availability") == "closed" else row["decision"])
            if status == "archived" or decision not in {"needs_review", "recommend"}:
                continue
            try:
                refs = json.loads(row["evidence_lines"] or "[]")
            except (ValueError, TypeError):
                refs = []
            doc = json.loads(row["document"])
            if question_if_open({**doc, "evidence_lines": refs if isinstance(refs, list) else []},
                                decision, fit, row["reason"] or ""):
                stored[row["id"]] = doc
    locators = {i: locator for i, doc in stored.items() if (locator := source_locator(doc))
                and locator.source == "inthiswork" and locator.original_id == str(i)}
    ids = sorted(locators)
    unsupported = sorted(stored.keys() - locators.keys())
    checked = {}
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        posts, _ = fetch_page(include_url(batch))
        valid = checked_inthiswork_posts(posts, {i: locators[i] for i in batch}, strict=True)
        for post_id, doc in valid.items():
            checked[post_id] = doc, inspect_document(doc)
    # Fetch every batch before starting the write transaction. A failed request preserves previous facts.
    with closing(sqlite3.connect(Path(state_db).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)) as db, db:
        db.execute("ATTACH DATABASE ? AS source", (Path(jobs_db).resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN IMMEDIATE")
        supports_linked = has_table(db, "job_linked_deadlines")
        for post_id, (doc, facts) in checked.items():
            row = db.execute("SELECT document FROM source.posts WHERE id=?", (post_id,)).fetchone()
            if not row or json.loads(row[0]) != stored[post_id]:
                continue
            write_facts(db, post_id, stored[post_id], facts, now, doc.get("modified_gmt"),
                        API + "/" + str(post_id), linked=_plan(doc).locator if supports_linked else None)
    return {"requested": len(ids), "checked": len(checked), "unsupported_ids": unsupported,
            "missing_ids": sorted(set(ids) - checked.keys()),
            "expired_in_checked": [i for i, (doc, _) in checked.items() if availability(doc, now=now)["is_expired"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-db", type=Path, default=ROOT / "data/jobs.sqlite")
    parser.add_argument("--state-db", type=Path, default=ROOT / "data/dashboard/dashboard.sqlite")
    args = parser.parse_args()
    print(json.dumps(refresh(args.jobs_db, args.state_db), ensure_ascii=False))


if __name__ == "__main__":
    main()
