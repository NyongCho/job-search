"""Verify and cache company logos independently of collected job documents."""

import argparse
import io
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .card_metadata import has_table
from .evidence import Document
from .job_availability import document_version
from .job_sources import company_key, company_names, fetch_posting, linked_job_locator, role_key, source_locator
from .public_fetch import PublicHTTPError, fetch_public

MAX_PAGE = 2_000_000
MAX_IMAGE = 1_000_000
DEFAULT_LOGO_LIMIT = 20


@dataclass(frozen=True)
class Candidate:
    key: str
    source_page: str
    image_url: str


@dataclass(frozen=True)
class LogoPlan:
    company: str
    role: str
    identity: str
    body: Candidate | None = None
    page: str | None = None
    domain: str | None = None
    locator: object = None


def _http_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) <= 32 or char == "\\" for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.hostname and parsed.username is None and parsed.scheme in {"http", "https"}:
            if parsed.port in (None, 443 if parsed.scheme == "https" else 80):
                return value
    except ValueError:
        pass
    return None


def _asset(url, base):
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        absolute = _http_url(urljoin(base, url.strip()))
    except ValueError:
        return None
    if not absolute:
        return None
    location = urlsplit(absolute)
    path = location.path.casefold()
    if ((location.hostname == "static.wanted.co.kr" and re.search(r"/images/wdes/[0-9]+_[0-9]+\.", path))
            or re.search(r"(?:^|/)(?:default[-_]?logo|logo[-_]?default|placeholder|"
                         r"wanted[-_]?logo|greetinghr[-_]?logo|greenhouse[-_]?logo|ascent[-_]?logo)[-_.\/]", path)):
        return None
    return absolute


def _logo_in_images(images, company, page):
    if not isinstance(images, list):
        return None
    aliases = company_names(company)
    choices = set()
    for item in images:
        if not isinstance(item, dict) or not isinstance(item.get("alt"), str):
            continue
        alt = re.fullmatch(r"\s*(.+?)\s*(?:공식\s*)?(?:로고|logo)\s*", item["alt"], re.I)
        if alt and company_key(alt[1]) in aliases and (url := _asset(item.get("url"), page)):
            choices.add(url)
    return next(iter(choices)) if len(choices) == 1 else None


def _official_domain(url, company):
    parsed = urlsplit(url)
    labels = parsed.hostname.lower().rstrip(".").split(".")
    suffix = ".".join(labels[-2:])
    size = 3 if suffix in {"co.kr", "or.kr", "com.au", "co.uk", "co.jp", "com.sg"} else 2
    if len(labels) < size or role_key(labels[-size]) not in company_names(company):
        return None
    return ".".join(labels[-size:])


def _plan(doc):
    title = doc.get("title")
    company, _, role = title.partition("｜") if isinstance(title, str) else ("", "", "")
    if not company_key(company) or not role_key(role):
        return LogoPlan("", "", "")
    source = _http_url(doc.get("url"))
    body_url = _logo_in_images(doc.get("images"), company, source) if source else None
    linked = doc.get("links")
    linked = linked if isinstance(linked, list) else []
    apply_url = _http_url(doc.get("apply_url"))
    link_labels = {"공식채용페이지", "채용페이지", "공고원문", "공고상세", "지원하러가기", "지원하기", "applynow"}
    urls = [source, apply_url]
    urls += [_http_url(link.get("url")) for link in linked[:24]
             if isinstance(link, dict) and role_key(link.get("text")) in link_labels]
    locator, page, domain = None, None, None
    saved_locator = source_locator(doc)
    if saved_locator and saved_locator.source != "inthiswork":
        locator = saved_locator
    else:
        for url in dict.fromkeys(urls):
            if not url:
                continue
            locator = linked_job_locator(doc, url)
            domain = _official_domain(url, company)
            if locator or domain:
                page = url
                break
    if locator:
        page = locator.url
        scope = f"{locator.source}:{locator.scope}"
        if locator.source in {"ascent", "wanted"}:
            scope += ":" + locator.original_id
    elif domain:
        scope = "domain:" + domain
    elif apply_url and urlsplit(apply_url).hostname not in {"forms.gle", "docs.google.com", "tally.so", "form.naver.com"}:
        page = apply_url
        scope = "linked:" + urlsplit(page).hostname
    elif body_url:
        scope = "body:" + body_url
    else:
        scope = ""
    identity = f"{scope}:{company_key(company)}" if scope else ""
    body = Candidate(identity, source, body_url) if body_url else None
    return LogoPlan(company, role, identity, body, page, domain, locator)


def _json_logo(page, plan, url):
    matches = set()
    for script in re.findall(r"""<script\b[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script\s*>""",
                             page, re.I | re.S):
        try:
            data = json.loads(script)
        except ValueError:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("@type") == "JobPosting" and role_key(entry.get("title")) == role_key(plan.role):
                organization = entry.get("hiringOrganization")
            else:
                organization = entry if entry.get("@type") == "Organization" else None
            if not isinstance(organization, dict) or not company_names(organization.get("name")) & company_names(plan.company):
                continue
            logo = organization.get("logo")
            if isinstance(logo, dict):
                logo = logo.get("url") or logo.get("contentUrl")
            if candidate := _asset(logo, url):
                matches.add(candidate)
    return next(iter(matches)) if len(matches) == 1 else None


def _official(plan, fetch, deadline):
    if not plan.page:
        return None
    if plan.locator:
        posting = fetch_posting(plan.locator, fetch, deadline)
        if plan.locator.source != "greenhouse":
            logo = _asset(posting.logo, plan.page)
            return Candidate(posting.organization, plan.page, logo) if logo else None
    final, raw, _ = fetch(plan.page, deadline=deadline, limit=MAX_PAGE)
    if plan.locator:
        match = linked_job_locator({"title": plan.company + "｜" + plan.role}, final)
        if not match or (match.scope, match.original_id) != (plan.locator.scope, plan.locator.original_id):
            return None
    else:
        if not _http_url(final):
            return None
        if plan.domain:
            if _official_domain(final, plan.company) != plan.domain:
                return None
        elif urlsplit(final).hostname != urlsplit(plan.page).hostname:
            return None
    try:
        page = raw.decode("utf-8-sig")
        doc = Document(page, final)
    except (ValueError, UnicodeError):
        return None
    logo = _logo_in_images(doc.images, plan.company, final) if plan.domain or plan.locator else None
    logo = logo or _json_logo(page, plan, final)
    return Candidate(plan.identity, final, logo) if logo else None


def _verified_png(raw, image_module):
    if not raw or len(raw) > MAX_IMAGE:
        return None
    formats = ("PNG", "JPEG", "WEBP", "GIF")
    try:
        with image_module.open(io.BytesIO(raw), formats=formats) as image:
            width, height = image.size
            if not (10 <= width <= 2048 and 10 <= height <= 2048 and width * height <= 2_000_000
                    and 0.45 <= width / height <= 12 and not getattr(image, "is_animated", False)):
                return None
            image.verify()
        with image_module.open(io.BytesIO(raw), formats=formats) as image, image.convert("RGBA") as raster:
            raster.thumbnail((256, 256))
            raster.info.clear()
            result = io.BytesIO()
            raster.save(result, format="PNG")
            return result.getvalue()
    except (OSError, SyntaxError, ValueError, image_module.DecompressionBombError):
        return None


def enqueue_logo(db, post_id, doc, fingerprint):
    identity = _plan(doc).identity
    db.execute("""INSERT INTO job_company_logos(post_id,fingerprint,document_version,identity,outcome)
          VALUES(?,?,?,?,?)
        ON CONFLICT(post_id) DO UPDATE SET fingerprint=excluded.fingerprint,
          document_version=excluded.document_version, identity=excluded.identity,
          outcome=excluded.outcome, attempted_at=NULL, lease_until=0,
          company_key=CASE WHEN job_company_logos.identity=excluded.identity THEN job_company_logos.company_key END""",
        (post_id, fingerprint, document_version(doc), identity, "pending" if identity else "missing"))


def _seed(db, limit):
    db.execute("BEGIN IMMEDIATE")
    rows = db.execute("""SELECT p.id,p.document,p.fingerprint FROM posts p LEFT JOIN job_company_logos j ON p.id=j.post_id
        WHERE p.id>0 AND (j.post_id IS NULL OR p.fingerprint<>j.fingerprint) ORDER BY p.id LIMIT ?""", (limit,)).fetchall()
    for post_id, serialized, fingerprint in rows:
        try:
            doc = json.loads(serialized)
        except ValueError:
            doc = {}
        enqueue_logo(db, post_id, doc if isinstance(doc, dict) else {}, fingerprint)
    db.commit()
    return len(rows)


def _attempt(db, plan, fetch, deadline, image_module):
    retry = False
    for current in ("body", "official"):
        try:
            candidate = plan.body if current == "body" else _official(plan, fetch, deadline)
            if not candidate:
                continue
            existing = db.execute("SELECT image_url FROM company_logos WHERE company_key=?", (candidate.key,)).fetchone()
            if existing and existing[0] == candidate.image_url:
                return "ready", candidate, None
            _, data, _ = fetch(candidate.image_url, deadline=deadline, limit=MAX_IMAGE)
            if (png := _verified_png(data, image_module)) is not None:
                return "ready", candidate, png
        except PublicHTTPError as error:
            retry |= error.status not in {404, 410}
        except OSError:
            retry = True
        except ValueError:
            continue
    return ("pending" if retry else "missing"), None, None


# A nonzero lease marks an interrupted or in-flight attempt, regardless of the cutoff.
_RETRY_ELIGIBLE = "(j.lease_until>0 OR ? IS NULL OR j.attempted_at IS NULL OR j.attempted_at<?)"


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Logo limit must be between 1 and 500")


def _batch_deadline(limit):
    return time.monotonic() + min(120, 6 * limit)


def _refresh_batch(db, *, limit, deadline, fetch, image_module, retry_before=None):
    summary = {"checked": 0, "ready": 0, "missing": 0, "retry": 0}
    ids = db.execute(f"""SELECT j.post_id FROM job_company_logos j JOIN posts p ON p.id=j.post_id
        WHERE j.outcome='pending' AND j.lease_until<=? AND {_RETRY_ELIGIBLE}
        ORDER BY attempted_at IS NOT NULL, attempted_at, post_id LIMIT ?""", (time.time(), retry_before, retry_before, limit)).fetchall()
    for (post_id,) in ids:
        if time.monotonic() >= deadline:
            break
        started = time.time()
        with db:
            attempt = db.execute(f"""UPDATE job_company_logos AS j SET attempted_at=?,lease_until=?
                WHERE post_id=? AND outcome='pending' AND lease_until<=? AND {_RETRY_ELIGIBLE}
                RETURNING document_version""",
                (started, started + 15, post_id, started, retry_before, retry_before)).fetchone()
            if not attempt:
                continue
            post = db.execute("SELECT document,fingerprint FROM posts WHERE id=?", (post_id,)).fetchone()
            if post is None:
                continue
            serialized, fingerprint = post
            try:
                doc = json.loads(serialized)
            except ValueError:
                doc = None
            if not isinstance(doc, dict) or document_version(doc) != attempt[0]:
                enqueue_logo(db, post_id, doc if isinstance(doc, dict) else {}, fingerprint)
                continue
        outcome, candidate, png = _attempt(db, _plan(doc), fetch, min(deadline, time.monotonic() + 5), image_module)
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("""SELECT p.document, j.attempted_at FROM posts p JOIN job_company_logos j ON j.post_id=p.id
                WHERE p.id=? AND j.document_version=?""", (post_id, attempt[0])).fetchone()
            if not current or current != (serialized, started):
                continue
            if png is not None:
                db.execute("""INSERT INTO company_logos VALUES(?,?,?,?,?)
                    ON CONFLICT(company_key) DO UPDATE SET source_page=excluded.source_page,image_url=excluded.image_url,
                      image=excluded.image,checked_at=excluded.checked_at WHERE excluded.checked_at>=company_logos.checked_at""",
                    (candidate.key, candidate.source_page, candidate.image_url, png, started))
            # Preserve the round boundary if the wall clock moved backward.
            attempted = max(started, retry_before) if retry_before is not None else started
            db.execute("""UPDATE job_company_logos SET company_key=COALESCE(?,company_key), outcome=?,
                attempted_at=?,lease_until=0 WHERE post_id=?""",
                (candidate.key if candidate else None, outcome, attempted, post_id))
        summary["checked"] += 1
        summary["retry" if outcome == "pending" else outcome] += 1
    return summary


def refresh_logos(path, *, limit=DEFAULT_LOGO_LIMIT, deadline=None, fetch=fetch_public, seed=True):
    _validate_limit(limit)
    empty = {"checked": 0, "ready": 0, "missing": 0, "retry": 0}
    deadline = deadline if deadline is not None else _batch_deadline(limit)
    if time.monotonic() >= deadline:
        return empty
    try:
        from PIL import Image
    except ImportError:
        return empty
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=0.2)) as db:
        if not has_table(db, "job_company_logos"):
            return empty
        if seed:
            _seed(db, limit)
        return _refresh_batch(db, limit=limit, deadline=deadline, fetch=fetch, image_module=Image)


def drain_logos(path, *, limit=DEFAULT_LOGO_LIMIT, retry_before, fetch=fetch_public):
    _validate_limit(limit)
    from PIL import Image

    summary = {"checked": 0, "ready": 0, "missing": 0, "retry": 0}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=0.2)) as db:
        for table in ("posts", "job_company_logos", "company_logos"):
            if not has_table(db, table):
                raise sqlite3.OperationalError(f"Required table is missing: {table}")
        while True:
            deadline = _batch_deadline(limit)
            with db:
                seeded = _seed(db, limit)
            batch = _refresh_batch(db, limit=limit, deadline=deadline, fetch=fetch,
                                   image_module=Image, retry_before=retry_before)
            for key, count in batch.items():
                summary[key] += count
            if seeded or batch["checked"]:
                continue
            lease = db.execute(f"""SELECT MIN(j.lease_until) FROM job_company_logos j JOIN posts p ON p.id=j.post_id
                WHERE j.outcome='pending' AND {_RETRY_ELIGIBLE}""", (retry_before, retry_before)).fetchone()[0]
            if lease is None:
                return summary
            delay = lease - time.time()
            if delay > 0:
                time.sleep(delay)


def logo_references(db):
    if not has_table(db, "job_company_logos"):
        return {}
    return dict(db.execute("""SELECT j.post_id,j.document_version FROM job_company_logos j
        JOIN company_logos c ON c.company_key=j.company_key"""))


def read_logo(path, post_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if not has_table(db, "job_company_logos"):
            return None
        row = db.execute("""SELECT p.document,j.document_version,c.image FROM job_company_logos j
            JOIN company_logos c ON c.company_key=j.company_key JOIN posts p ON p.id=j.post_id WHERE p.id=?""",
            (post_id,)).fetchone()
    if not row:
        return None
    try:
        if document_version(json.loads(row[0])) == row[1]:
            return row[2]
    except ValueError:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=DEFAULT_LOGO_LIMIT)
    parser.add_argument("--drain", action="store_true", help="Process all available logo requests in batches")
    args = parser.parse_args()
    if args.drain:
        summary = drain_logos(args.db, limit=args.limit, retry_before=time.time(), fetch=fetch_public)
    else:
        summary = refresh_logos(args.db, limit=args.limit, fetch=fetch_public)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
