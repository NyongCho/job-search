"""Manual source and logo refresh workers. External I/O runs outside write transactions."""

import json
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import collect_jobs, company_logos, refresh_queue
from .job_availability import _automatic, _stamp, document_version, has_jd_content, inspect_document
from .logo_scope import eligible_post_ids
from .job_sources import SourceLocator, matches_jd_source, parse_posting, role_key, source_locator
from .public_fetch import fetch_public
from .refresh_job_availability import (
    _external_facts,
    accepts_facts,
    checked_inthiswork_posts,
    include_url,
    write_facts,
)

UNAVAILABLE = "같은 직무의 공개 공고를 확인하지 못했어요. 마지막 확인값을 유지했어요."
CHANGED = "확인 중 공고가 바뀌어 최신 저장값을 유지했어요."
NO_SOURCE = "이 공고에 연결된 자동 확인 경로가 없어요."


# Reserve the other public reader slots for logos. Concurrent claims can share a board response.
_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="refresh-source")
_IN_FLIGHT = {}
_READ_LOCK = threading.Lock()
# Keep at most 16 official page responses (each limited to MAX_PAGE) across worker claims.
_RECENT_PAGES = OrderedDict()


def _read(fetch, url, scopes, previous):
    key = (fetch, url)
    is_batch = url.startswith(collect_jobs.API + "?")
    cache_keys = [] if is_batch else [(scope, key) for scope in scopes]

    def retrieve():
        return fetch(
            url,
            deadline=time.monotonic() + (8 if is_batch else 6),
            limit=12_000_000 if is_batch else company_logos.MAX_PAGE,
        )

    def discard(done):
        with _READ_LOCK:
            if _IN_FLIGHT.get(key) is done:
                del _IN_FLIGHT[key]

    with _READ_LOCK:
        future = previous or next((_RECENT_PAGES[k] for k in cache_keys if k in _RECENT_PAGES), None)
        if future is None:
            active = _IN_FLIGHT.get(key)
            if active is not None and not active.done():
                future = active
        started = future is None
        if started:
            future = _IN_FLIGHT[key] = _READERS.submit(retrieve)
        for cache_key in cache_keys:
            _RECENT_PAGES[cache_key] = future
            _RECENT_PAGES.move_to_end(cache_key)
        while len(_RECENT_PAGES) > 16:
            _RECENT_PAGES.popitem(last=False)
    if started:
        future.add_done_callback(discard)
    return future


@dataclass
class SourceResult:
    facts: dict | None
    modified: str | None
    jd: dict | None
    plan: company_logos.LogoPlan
    linked_listing: dict | None = None
    official_facts: dict | None = None
    official_modified: str | None = None


@dataclass
class Selection:
    task: dict
    document: dict
    locator: SourceLocator
    listing: SourceResult | None = None


def _open(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=10)


def _official_result(doc, locator, final_url, raw, *, listing=None):
    posting = parse_posting(locator, final_url, raw, previous=doc)
    try:
        official_facts = _external_facts(doc, posting)
    except ValueError:
        official_facts = None
    facts = listing.facts if listing else official_facts
    if listing and official_facts is None and not facts["closed"] and facts["deadline_kind"] == "unknown":
        facts = None
    modified = listing.modified if listing else posting.modified
    plan = listing.plan if listing else company_logos._plan(doc)
    fresh = listing.jd if listing else None
    origin = {"source": locator.source, "url": locator.url, "original_id": locator.original_id}
    previous = _stamp(doc.get("jd_modified_gmt")) if matches_jd_source(doc, locator) else None
    incoming = _stamp(posting.modified)
    body = posting.body
    if has_jd_content({"lines": body.lines, "images": body.images, "links": body.links}) and (
        not previous or (incoming and incoming > previous)
    ):
        lines = body.lines.copy()
        for image in body.images:
            alt = (image.get("alt") or "").strip()
            if alt and alt not in lines and not role_key(alt).endswith(("로고", "logo")):
                lines.append(alt)
        fresh = {
            **(listing.linked_listing if listing else doc),
            "lines": lines,
            "images": body.images,
            "jd_source": origin,
            "jd_organization": posting.organization,
            "content_status": "text_available" if len("".join(lines)) >= 80 else "needs_review",
        }
        if listing:
            fresh["listing_lines"] = listing.linked_listing["lines"]
            fresh["listing_images"] = listing.linked_listing["images"]
        if modified:
            fresh["modified_gmt"] = modified
        if posting.modified:
            fresh["jd_modified_gmt"] = posting.modified
    board = None
    if locator.source == "greeting":
        # Save the branding from this response so the logo worker need not download the posting again.
        board = company_logos._board_brand(raw.decode("utf-8-sig"), final_url, locator.company)
        plan = replace(plan, page=None)
    candidate = board or company_logos.posting_logo(posting, locator) or plan.body
    return SourceResult(
        facts,
        modified,
        fresh,
        replace(plan, body=candidate, verified_source=True, organization=posting.organization),
        official_facts=official_facts if listing else None,
        official_modified=posting.modified,
    )


def _inthiswork_result(doc, fresh):
    modified = fresh["modified_gmt"]
    saved = _stamp(doc.get("modified_gmt"))
    has_jd = has_jd_content(fresh)
    revised = None
    if not saved or _stamp(modified) > saved:
        revised = collect_jobs.preserve_jd(doc, fresh)
    elif has_jd and not has_jd_content(doc):
        revised = fresh
    return SourceResult(
        inspect_document(fresh),
        modified,
        revised,
        company_logos._plan(fresh),
        linked_listing=fresh if doc.get("jd_source") or not has_jd else None,
    )


def _preserve_checked_deadline(db, post_id, previous, current):
    cached = db.execute(
        """SELECT a.*,f.document_version,f.deadline_kind,f.deadline_word FROM job_availability a
        LEFT JOIN job_deadline_facts f ON f.post_id=a.post_id AND f.checked_at=a.checked_at
        WHERE a.post_id=?""",
        (post_id,),
    ).fetchone()
    if cached:
        facts, checked = _automatic(previous, dict(cached))
        if checked:
            db.execute(
                "INSERT OR REPLACE INTO job_deadline_facts VALUES(?,?,?,?,?)",
                (post_id, checked, document_version(current), facts["deadline_kind"], facts["deadline_word"]),
            )


def _publish(db, task, doc, locator, result, started, finish, *, awaiting_official=False):
    post_id = task["post_id"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT document,fingerprint FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row or row["fingerprint"] != task["version"] or json.loads(row["document"]) != doc:
            finish(task, "skipped", CHANGED, db=db)
            return False
        if not accepts_facts(db, post_id, doc, started, result.modified):
            finish(task, "done", "더 최근에 확인한 값이 있어 유지했어요.", db=db)
            return False
        version = row["fingerprint"]
        if not awaiting_official and result.jd is not None and result.jd != doc:
            previous = doc
            doc = {**result.jd, "id": post_id}
            version = collect_jobs.save_document(db, doc)
            if result.facts is None:
                _preserve_checked_deadline(db, post_id, previous, doc)
        publish_facts = result.facts is not None and (
            not awaiting_official or result.facts["closed"] or result.facts["deadline_kind"] != "unknown"
        )
        if publish_facts:
            linked = result.plan.locator if locator.source == "inthiswork" else None
            write_facts(db, post_id, doc, result.facts, started, result.modified, locator.endpoint,
                        linked=linked, official_facts=result.official_facts, official_modified=result.official_modified)
        if locator.source == "inthiswork" and result.plan.locator:
            revision_key = collect_jobs.PENDING_LISTING_REVISION + str(post_id)
            if awaiting_official and publish_facts and result.jd is not None:
                # Collection can arrive between publishing this deadline and committing the linked JD.
                db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (revision_key, result.modified))
            elif not awaiting_official:
                db.execute("DELETE FROM metadata WHERE key=? AND value=?", (revision_key, result.modified))
        if not awaiting_official or result.plan.body:
            plan = replace(result.plan, page=None, awaiting_official=True) if awaiting_official else result.plan
            db.execute(
                """INSERT OR REPLACE INTO refresh_source_snapshots VALUES(?,?,?,?,?)""",
                (task["id"], post_id, task["version"], version, json.dumps(asdict(plan), ensure_ascii=False)),
            )
        if awaiting_official:
            if publish_facts:
                refresh_queue.progress(db, task)
            return True
        finished = {**task, "applied_version": version}
        if result.facts is None:
            finish(finished, "failed", UNAVAILABLE, db=db)
        else:
            finish(finished, "done", db=db)


def _initial_requests(db, tasks, finish):
    inthiswork, pages = [], defaultdict(list)
    for task in tasks:
        if task["post_id"] < 0:
            finish(task, "skipped", NO_SOURCE)
            continue
        row = db.execute("SELECT document,fingerprint FROM posts WHERE id=?", (task["post_id"],)).fetchone()
        if not row or row["fingerprint"] != task["version"]:
            finish(task, "skipped", CHANGED)
            continue
        try:
            doc = json.loads(row["document"])
            locator = source_locator(doc) if isinstance(doc, dict) else None
        except (TypeError, ValueError):
            locator = None
        if not locator or (locator.source == "inthiswork" and locator.original_id != str(task["post_id"])):
            finish(task, "skipped", NO_SOURCE)
        elif (any(not isinstance(doc.get(field, []), list) for field in ("lines", "links", "images"))
              or any(not isinstance(line, str) for line in doc.get("lines", []))
              or any(isinstance(link, dict) and not isinstance(link.get("text", ""), str)
                     for link in doc.get("links", []))):
            finish(task, "failed", "저장된 공고 본문 형식을 확인할 수 없어요.")
        elif locator.source == "inthiswork":
            inthiswork.append(Selection(task, doc, locator))
        else:
            pages[locator.endpoint].append(Selection(task, doc, locator))
    for start in range(0, len(inthiswork), 100):
        batch = inthiswork[start : start + 100]
        yield include_url(sorted({s.task["post_id"] for s in batch})), batch
    yield from pages.items()


def _read_results(future, group):
    batch = group[0].locator.source == "inthiswork"
    valid = {}
    try:
        final, raw, _ = future.result()
        if batch:
            if (
                urlsplit(final).hostname != "inthiswork.com"
                or urlsplit(final).path != urlsplit(collect_jobs.API).path
            ):
                raise ValueError("Inthiswork API redirected away from collection")
            locators = {s.task["post_id"]: s.locator for s in group}
            valid = checked_inthiswork_posts(json.loads(raw), locators, strict=False)
    except (OSError, TypeError, ValueError):
        raw = None
    for selection in group:
        if batch:
            fresh = valid.get(selection.task["post_id"])
            result = _inthiswork_result(selection.document, fresh) if fresh else None
        else:
            listing = selection.listing
            try:
                if raw is None:
                    raise ValueError("Source unavailable")
                result = _official_result(selection.document, selection.locator, final, raw, listing=listing)
            except (TypeError, ValueError):
                if listing:
                    facts = listing.facts
                    if not facts["closed"] and facts["deadline_kind"] == "unknown":
                        facts = None
                    # A closed or unverifiable job can still have independently verifiable employer branding.
                    home = company_logos.greeting_home(selection.locator.url)
                    result = replace(listing, facts=facts, plan=replace(listing.plan, page=home))
                else:
                    result = None
        yield selection, result


def refresh_deadlines(jobs_path, state_path, tasks, finish, *, fetch=fetch_public):
    started = datetime.now(timezone.utc)
    reads, pending = {}, defaultdict(list)
    state = Path(state_path).resolve(strict=True)

    def schedule(url, group):
        scopes = {(state, item.task["run_id"]) for item in group if "run_id" in item.task}
        reads[url] = _read(fetch, url, scopes, reads.get(url))
        pending[reads[url]].extend(group)

    with closing(_open(jobs_path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ? AS dashboard", (str(state),))
        for url, group in _initial_requests(db, tasks, finish):
            schedule(url, group)
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                for selection, result in _read_results(future, pending.pop(future)):
                    if result is None:
                        finish(selection.task, "failed", UNAVAILABLE)
                        continue
                    if selection.locator.source == "inthiswork" and result.plan.locator and result.linked_listing:
                        if _publish(
                            db,
                            selection.task,
                            selection.document,
                            selection.locator,
                            result,
                            started,
                            finish,
                            awaiting_official=True,
                        ):
                            linked = replace(selection, locator=result.plan.locator, listing=result)
                            schedule(linked.locator.endpoint, [linked])
                    else:
                        _publish(
                            db,
                            selection.task,
                            selection.document,
                            source_locator(selection.document),
                            result,
                            started,
                            finish,
                        )


def _snapshot_plan(serialized):
    data = json.loads(serialized)
    if data["locator"]:
        data["locator"] = SourceLocator(**data["locator"])
        if not data.get("page") and not data.get("awaiting_official") and not data.get("verified_source"):
            # Previously saved final snapshots omitted the board URL when the linked posting failed.
            data["page"] = company_logos.greeting_home(data["locator"].url)
    if data["body"]:
        data["body"] = company_logos.Candidate(**data["body"])
    return company_logos.LogoPlan(**data)


def _prepare_logo(db, task):
    post_id, version = task["post_id"], task["version"]
    receipt = db.execute(
        """SELECT status FROM refresh_logo_receipts
        WHERE work_id=? AND post_id=? AND version=?""",
        (task["id"], post_id, version),
    ).fetchone()
    if receipt:
        return receipt[0], version, None
    snapshot = db.execute(
        """SELECT applied_version,logo_plan FROM refresh_source_snapshots
        WHERE work_id=? AND post_id=? AND version=?""",
        (task.get("source_work_id"), post_id, version),
    ).fetchone()
    post = db.execute("SELECT document,fingerprint FROM posts WHERE id=?", (post_id,)).fetchone()
    expected = snapshot[0] if snapshot else version
    if not post or post[1] != expected:
        return "skipped", expected, None
    doc = json.loads(post[0])
    plan = _snapshot_plan(snapshot[1]) if snapshot else company_logos._plan(doc)
    company = company_logos.cached_company(db, post_id, doc, plan)
    if company:
        row = db.execute("SELECT document_version FROM job_company_logos WHERE post_id=?", (post_id,)).fetchone()
        if not row or row[0] != document_version(doc):
            company_logos.enqueue_logo(db, post_id, doc, expected)
        db.execute(
            """UPDATE job_company_logos SET company_key=?,outcome='ready',lease_until=0,
            identity=CASE WHEN ? THEN ? ELSE identity END WHERE post_id=?""",
            (company, plan.awaiting_official, plan.identity, post_id),
        )
        company_logos.record_logo_receipt(db, task, "done")
        return "done", expected, None
    if task.get("source_work_id") and not snapshot:
        if task.get("source_status") in ("queued", "pending", "running", None):
            return "waiting", expected, None
        # A failed or superseded direct posting was just checked by the deadline worker.
        if source_locator(doc) == plan.locator and plan.locator:
            plan = replace(plan, page=company_logos.greeting_home(plan.page) if plan.page else None)
    return None, expected, plan


def _publish_shared_logo(db, state_path, task):
    """Complete earlier logo misses in the same refresh once their verified employer has a PNG."""
    if not task.get("run_id"):
        return
    db.execute("ATTACH DATABASE ? AS dashboard", (str(Path(state_path).resolve(strict=True)),))
    with db:
        db.execute("BEGIN IMMEDIATE")
        company = db.execute("""SELECT c.company_key FROM refresh_source_snapshots s
            JOIN posts p ON p.id=s.post_id AND p.fingerprint=s.applied_version
            JOIN company_logos c ON c.company_key=json_extract(s.logo_plan,'$.organization')
            WHERE s.work_id=? AND json_extract(s.logo_plan,'$.verified_source')""",
            (task["source_work_id"],)).fetchone()
        if not company:
            return
        rows = db.execute("""SELECT DISTINCT w.id,w.post_id FROM refresh_members m
            JOIN refresh_work w ON w.group_id=m.group_id AND w.kind='logo'
            JOIN refresh_logo_receipts r ON r.work_id=w.id AND r.status IN ('skipped','failed')
            JOIN refresh_work d ON d.group_id=w.group_id AND d.kind='deadline'
            JOIN refresh_source_snapshots s ON s.work_id=d.id AND s.version=r.version
            JOIN posts p ON p.id=w.post_id AND p.fingerprint=s.applied_version
            JOIN job_company_logos j ON j.post_id=p.id AND j.fingerprint=p.fingerprint
            WHERE m.run_id IN (SELECT subscribed.run_id FROM refresh_members subscribed
                JOIN refresh_work current USING(group_id) WHERE current.id=?)
            AND w.status IN ('skipped','failed','running') AND (j.company_key IS NULL OR j.company_key=?)
            AND json_extract(s.logo_plan,'$.verified_source') AND json_extract(s.logo_plan,'$.organization')=?""",
            (task["id"], company[0], company[0])).fetchall()
        for work_id, post_id in rows:
            db.execute("UPDATE job_company_logos SET company_key=?,outcome='ready',lease_until=0 WHERE post_id=?",
                       (company[0], post_id))
            db.execute("UPDATE refresh_logo_receipts SET status='done' WHERE work_id=?", (work_id,))
            db.execute("UPDATE refresh_work SET status='done',message='' WHERE id=?", (work_id,))
            db.execute("INSERT OR REPLACE INTO refresh_changes(work_id) VALUES(?)", (work_id,))


def refresh_logo_task(jobs_path, state_path, task, finish, *, fetch=fetch_public):
    """Return False when a source result or a concurrent logo download is still pending."""
    if task["post_id"] < 0:
        finish(task, "skipped", NO_SOURCE)
        return True
    if not eligible_post_ids(jobs_path, state_path, task["post_id"]):
        finish(task, "skipped", refresh_queue.LOGO_OUT_OF_SCOPE)
        return True
    from PIL import Image

    with closing(_open(jobs_path)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            status, expected, plan = _prepare_logo(db, task)
        if status == "waiting":
            return False
        if status is None:
            outcome = company_logos._refresh_post(
                db,
                task["post_id"],
                deadline=time.monotonic() + 6,
                fetch=fetch,
                image_module=Image,
                work=task,
                expected_version=expected,
                plan=plan,
            )
            if outcome is None:
                return False
            status = {"ready": "done", "pending": "failed", "missing": "skipped"}[outcome]
        _publish_shared_logo(db, state_path, task)
    message = {
        "done": "",
        "failed": "로고를 가져오지 못했어요.",
        "skipped": "이 버전에 확인된 기업 로고가 없어요.",
    }
    finish(task, status, message[status])
    return True
