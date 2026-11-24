"""Prepare a stored, open image JD for normal assessment outside the HTTP request."""

import json
import time
from contextlib import closing, nullcontext
from pathlib import Path

import collect_jobs
from fit_work import _db, _facts
from job_availability import source_document
from job_sources import fetch_posting, has_review_text, linked_job_locator, official_jd
from poster_evidence import RETRY_SECONDS, enrich_document, poster_candidates
from public_fetch import fetch_public


def _official(jobs_path, doc, locator):
    # Reserve the check before I/O. A timeout can recover after a cooldown even if OCR succeeded.
    key = "poster_official:" + str(doc["id"])
    revision = [doc.get("modified_gmt"), locator.url]
    now = time.time()
    with closing(_db(jobs_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row:
            previous, after, closed = json.loads(row[0])
            if previous == revision and now < after:
                return None, closed
        reservation = json.dumps([revision, now + RETRY_SECONDS, False])
        db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, reservation))
    try:
        posting = fetch_posting(locator, fetch_public, time.monotonic() + 6, previous=doc)
    except (OSError, TypeError, ValueError, KeyError):
        return None, False
    if posting.closed:
        with closing(_db(jobs_path)) as db, db:
            db.execute("UPDATE metadata SET value=? WHERE key=? AND value=?",
                       (json.dumps([revision, now + RETRY_SECONDS, True]), key, reservation))
    return posting, posting.closed


def hold_reason(doc):
    """A missing or unreadable source cannot establish an applicant question."""
    evidence = doc.get("poster_evidence") or {}
    if evidence:
        return "" if evidence.get("status") == "ready" else evidence["reason"]
    if poster_candidates(doc):
        return "채용 포스터의 본문 판독을 기다립니다."
    return "" if has_review_text(doc) else "평가에 필요한 채용 본문을 확인할 수 없습니다."


def prepare_post(jobs_path, state_path, post_id, *, allow_model=False, check_official=True):
    """Return a displayable hold reason, or an empty string when normal assessment may proceed."""
    with closing(_db(jobs_path, "ro")) as db:
        row = db.execute("SELECT document,fingerprint FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        return "공고가 존재하지 않습니다."
    doc = json.loads(row["document"])
    evidence = doc.get("poster_evidence")
    if not evidence and not hold_reason(doc):
        return ""
    state = closing(_db(state_path, "ro")) if state_path and Path(state_path).is_file() else nullcontext()
    with state as connection:
        if _facts(connection, doc)[0] == "closed":
            return "접수가 마감되어 포스터 판독을 건너뛰었습니다."

    fresh = doc
    if check_official and not doc.get("jd_source"):
        for link in collect_jobs._job_links(doc):
            locator = linked_job_locator(doc, link)
            if not locator:
                continue
            posting, closed = _official(jobs_path, doc, locator)
            if closed:
                return "공식 공고가 마감되어 포스터 판독을 건너뛰었습니다."
            if posting:
                fresh = official_jd(doc, locator, posting) or doc
            break
    if fresh.get("poster_evidence") or poster_candidates(fresh):
        fresh = enrich_document(jobs_path, fresh, allow_model=allow_model)

    hold = hold_reason(fresh)
    if fresh != doc:
        with closing(_db(jobs_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT document,fingerprint,decision,reviewed_at FROM posts WHERE id=?", (post_id,)).fetchone()
            if not current:
                return "공고가 존재하지 않습니다."
            if current["fingerprint"] != row["fingerprint"]:
                latest = json.loads(current["document"])
                if source_document(latest) == source_document(fresh):
                    # Local and explicit model results for one source can finish in either order.
                    if (latest.get("poster_evidence") or {}).get("status") == "ready" or hold:
                        return hold_reason(latest)
                elif source_document(latest) != source_document(doc) or not fresh.get("jd_source"):
                    return "판독 중 원문이 바뀌어 최신 공고의 평가를 기다립니다."
            if (current["decision"] or current["reviewed_at"]) and not collect_jobs.has_table(db, "fit_delivery"):
                # Fit migration treats already reviewed posts as delivered even on old installations.
                db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("poster_legacy_receipt:" + str(post_id), "1"))
            collect_jobs.save_document(db, fresh)
            if not hold:
                # A previously delivered posting may need a new fit without another notification.
                db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("poster_fit:" + str(post_id), "1"))
    return hold
