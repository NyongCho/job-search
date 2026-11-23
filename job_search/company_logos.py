"""Verify and cache company logos independently of collected job documents."""

import argparse
import io
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from .card_metadata import has_table
from .evidence import Document
from .job_availability import document_version
from .jobda_logos import jobda_logo
from .job_sources import (
    _next_data, company_key, company_names, fetch_posting, linked_job_locator, matches_jd_source, parse_posting,
    role_key, source_locator,
)
from .public_fetch import PublicHTTPError, fetch_public

MAX_PAGE = 2_000_000
MAX_IMAGE = 1_000_000
DEFAULT_LOGO_LIMIT = 20
_SIGNAL_NAME = re.compile(r"([0-9]{1,20})-[0-9a-f]{32}")


def _signal_directory(path):
    return Path(f"{path}.logo-requests")


def signal_logo_worker(path):
    signal = _signal_directory(path) / f"{time.time_ns()}-{uuid4().hex}"
    try:
        signal.touch(exist_ok=False)
    except FileNotFoundError:
        pass  # The signal directory is opt-in.
    except OSError as error:
        print(f"Could not signal company logo worker: {error}", file=sys.stderr)


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
    verified_source: bool = False
    organization: str | None = None
    awaiting_official: bool = False


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
    body_url = None
    if source:
        body_url = _logo_in_images(doc.get("listing_images"), company, source) or _logo_in_images(doc.get("images"), company, source)
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
    organization = doc.get("jd_organization")
    if not isinstance(organization, str) or not matches_jd_source(doc, locator):
        organization = None
    body = Candidate(organization or identity, source, body_url) if body_url else None
    return LogoPlan(company, role, identity, body, page, domain, locator, organization=organization)


def _brand_names(value):
    # Some board owners use their careers-site title as the workspace name.
    return company_names(re.sub(r"\s+(?:채용|careers)\s*$", "", value, flags=re.I)) if isinstance(value, str) else set()


def greeting_home(url):
    host = urlsplit(url).hostname
    if host and re.fullmatch(r"[a-z0-9-]+\.career\.greetinghr\.com", host):
        return "https://" + host + "/"
    return None


def _ashby_brand(page, url, path, employer):
    match = re.fullmatch(r"/([^/]+)/([^/]+)(?:/application)?", path)
    script = re.search(r"<script\b[^>]*>\s*window\.__appData\s*=\s*", page, re.I)
    if not match or not script:
        return None
    data, _ = json.JSONDecoder().raw_decode(page, script.end())
    org = data["organization"]
    if (org["hostedJobsPageSlug"] != match[1] or data["posting"]["id"] != match[2]
            or not employer & company_names(org["name"])):
        return None
    for field in ("logoSquareImageUrl", "logoWordmarkImageUrl"):
        asset = _asset(org["theme"].get(field), url)
        if (asset and urlsplit(asset).hostname == "app.ashbyhq.com"
                and re.fullmatch(r"/api/images/org-theme-(?:square|wordmark)/"
                                 + re.escape(org["organizationId"]) + r"/.+", urlsplit(asset).path)):
            return Candidate("ashby:" + org["organizationId"], url, asset)
    return None


def _ninehire_brand(props, url, employer):
    home = props["homepage"]
    info, domain = home["info"], home["domain"]
    company_id = info["companyId"]
    location = urlsplit(url)
    if (domain["hostname"] != location.hostname or urlsplit(domain["url"]).path.rstrip("/") != location.path.rstrip("/")
            or props["jobPosting"]["companyId"] != company_id or props["recruitment"]["companyId"] != company_id
            or not employer & _brand_names(info["companyName"])):
        return None
    asset = _asset((info["brand"].get("image") or {}).get("fileUrl"), url)
    if (asset and urlsplit(asset).hostname == "image.ninehire.com"
            and urlsplit(asset).path.startswith("/brand/" + company_id + "/")):
        return Candidate("ninehire:" + company_id, url, asset)
    return None


def _greeting_brand(props, url, employer):
    workspace = props["careerBaseInfo"]["workspace"]
    design = props.get("careerBootInfo", {}).get("design", {})
    location = urlsplit(url)
    owner_names = _brand_names(workspace["name"]) | _brand_names(design.get("footer", {}).get("companyName"))
    if props["host"] != location.hostname or not employer & owner_names:
        return None
    opening = re.fullmatch(r"/(?:[a-z]{2}/)?o/([0-9]+)/?", location.path)
    if opening and str(props.get("openingId")) != opening[1]:
        return None
    for value in (workspace.get("imageUrl"), design.get("header", {}).get("logoUrl")):
        asset = _asset(value, url)
        if not asset:
            continue
        loc = urlsplit(asset)
        if ((loc.hostname == "profiles.greetinghr.com" and loc.path.startswith("/group/"))
                or loc.hostname == "opening-attachments.greetinghr.com"):
            tenant = location.hostname.split(".")[0] if greeting_home(url) else f"{location.hostname}:{workspace['id']}"
            return Candidate("greeting:" + tenant, url, asset)
    return None


def _board_brand(page, url, company):
    """Read employer branding fields bound to the board serving the application link."""
    location = urlsplit(url)
    path = location.path.rstrip("/")
    employer = company_names(company)
    try:
        if location.hostname == "jobs.ashbyhq.com":
            return _ashby_brand(page, url, path, employer)
        if re.fullmatch(r"/job_posting/[^/]+", path):
            return _ninehire_brand(_next_data(page), url, employer)
        if greeting_home(url) or re.fullmatch(r"/[a-z]{2}/o/[0-9]+", path):
            return _greeting_brand(_next_data(page), url, employer)
    except (AttributeError, KeyError, TypeError, ValueError):
        pass  # Missing or inconsistent public branding metadata is not an employer match.
    return None


def _lever_name_matches(url, name, company):
    location = urlsplit(url)
    if location.hostname not in {"jobs.lever.co", "jobs.eu.lever.co"} or not isinstance(name, str):
        return False
    tenant = re.fullmatch(r"/([^/]+)/[^/]+(?:/apply)?/?", location.path)
    # The employer can appear as its tenant slug in listings and legal name in JobPosting.
    short = re.sub(r"\s+(?:technologies|inc\.?|corporation|corp\.?|ltd\.?)$", "", name, flags=re.I)
    return bool(tenant and role_key(tenant[1]) in company_names(company) & company_names(short))


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
            if not isinstance(organization, dict):
                continue
            name = organization.get("name")
            if not (company_names(name) & company_names(plan.company) or (entry.get("@type") == "JobPosting"
                    and _lever_name_matches(url, name, plan.company))):
                continue
            logo = organization.get("logo")
            if isinstance(logo, dict):
                logo = logo.get("url") or logo.get("contentUrl")
            if candidate := _asset(logo, url):
                matches.add(candidate)
    return next(iter(matches)) if len(matches) == 1 else None


def posting_logo(posting, locator):
    url = _asset(posting.logo, locator.url) or _logo_in_images(posting.body.images, locator.company, locator.url)
    return Candidate(posting.organization, locator.url, url) if url else None


def _official(plan, fetch, deadline, doc):
    if not plan.page:
        return None
    if plan.verified_source and plan.locator and plan.locator.source not in {"greenhouse", "greeting"}:
        return None
    if plan.locator and plan.locator.source != "greeting" and not plan.verified_source:
        posting = fetch_posting(plan.locator, fetch, deadline, previous=doc)
        candidate = posting_logo(posting, plan.locator)
        if candidate or plan.locator.source != "greenhouse":
            return candidate
    home = greeting_home(plan.page)
    requested = plan.page
    try:
        final, raw, _ = fetch(requested, deadline=deadline, limit=MAX_PAGE)
    except PublicHTTPError as error:
        if error.status not in {404, 410} or not home or requested == home:
            raise
        # Closed listings may disappear while the same employer still publishes its board.
        requested = home
        final, raw, _ = fetch(home, deadline=deadline, limit=MAX_PAGE)
    if home and requested == home:
        if greeting_home(final) != home:
            return None
        return _board_brand(raw.decode("utf-8-sig"), final, plan.company)
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
        document = Document(page, final)
    except (ValueError, UnicodeError):
        return None
    original_path = urlsplit(plan.page).path.rstrip("/")
    final_path = urlsplit(final).path.rstrip("/")
    same_greeting_opening = (home and greeting_home(final) == home
                             and re.sub(r"^/[a-z]{2}(?=/o/)", "", final_path) == original_path)
    if final_path == original_path or same_greeting_opening:
        candidate = _board_brand(page, final, plan.company)
        if candidate:
            return candidate
        image = jobda_logo(page, final, plan.company, fetch, deadline)
        if image:
            return Candidate("jobda:" + urlsplit(final).hostname, final, image)
    if plan.locator and plan.locator.source == "greeting" and not plan.verified_source:
        posting = parse_posting(plan.locator, final, raw, previous=doc)
        return posting_logo(posting, plan.locator)
    logo = _logo_in_images(document.images, plan.company, final) if plan.domain or plan.locator else None
    logo = logo or _json_logo(page, plan, final)
    key = f"{plan.locator.source}:{plan.locator.scope}" if plan.locator else plan.identity
    return Candidate(key, final, logo) if logo else None


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


class ImageClaimInProgress(Exception):
    pass


def _claim_image(db, candidate, deadline):
    token = uuid4().hex
    while True:
        with db:
            db.execute("BEGIN IMMEDIATE")
            cached = db.execute("SELECT image_url FROM company_logos WHERE company_key=?", (candidate.key,)).fetchone()
            if cached and cached[0] == candidate.image_url:
                return None
            now = time.time()
            claim = db.execute("""INSERT INTO logo_image_claims VALUES(?,?,?,?)
                ON CONFLICT(company_key,image_url) DO UPDATE SET token=excluded.token,lease_until=excluded.lease_until
                WHERE logo_image_claims.lease_until<=? RETURNING token""",
                (candidate.key, candidate.image_url, token, now + 15, now)).fetchone()
            if claim:
                return token
        if time.monotonic() >= deadline:
            raise ImageClaimInProgress
        time.sleep(.05)


def _release_image(db, token):
    if token:
        db.execute("DELETE FROM logo_image_claims WHERE token=?", (token,))


def _attempt(db, plan, fetch, deadline, image_module, doc):
    retry = False
    for current in ("body", "official"):
        token = None
        try:
            candidate = plan.body if current == "body" else _official(plan, fetch, deadline, doc)
            if not candidate:
                continue
            token = _claim_image(db, candidate, deadline)
            if token is None:
                return "ready", candidate, None, None
            final, data, _ = fetch(candidate.image_url, deadline=deadline, limit=MAX_IMAGE)
            if candidate.key.startswith("jobda:") and final != candidate.image_url:
                raise ValueError("JOBDA image redirected away from its verified asset")
            png = _verified_png(data, image_module)
            if png is not None:
                return "ready", candidate, png, token
        except ImageClaimInProgress:
            return "busy", None, None, None
        except PublicHTTPError as error:
            retry |= error.status not in {404, 410}
        except OSError:
            retry = True
        except ValueError:
            pass
        with db:
            _release_image(db, token)
    return ("pending" if retry else "missing"), None, None, None


def record_logo_receipt(db, work, status):
    db.execute("""INSERT OR IGNORE INTO refresh_logo_receipts(work_id,post_id,version,status)
        VALUES(?,?,?,?)""", (work["id"], work["post_id"], work["version"], status))


def cached_company(db, post_id, doc, plan):
    own = db.execute("""SELECT j.company_key FROM job_company_logos j JOIN company_logos c USING(company_key)
        WHERE j.post_id=? AND j.document_version=?""", (post_id, document_version(doc))).fetchone()
    if own:
        return own[0]
    if plan.organization:
        row = db.execute("SELECT company_key FROM company_logos WHERE company_key=?", (plan.organization,)).fetchone()
        if row:
            return row[0]
    if plan.body:
        row = db.execute("SELECT company_key FROM company_logos WHERE company_key=? AND image_url=?",
                         (plan.body.key, plan.body.image_url)).fetchone()
        if row:
            return row[0]
    if plan.domain or (plan.locator and plan.locator.source in {"greenhouse", "greeting"}):
        row = db.execute("""SELECT DISTINCT j.company_key FROM job_company_logos j JOIN company_logos c USING(company_key)
            WHERE j.identity=? LIMIT 2""", (plan.identity,)).fetchall()
        if len(row) == 1:
            return row[0][0]
    return None


# A nonzero lease marks an interrupted or in-flight attempt, regardless of the cutoff.
_RETRY_ELIGIBLE = "(j.lease_until>0 OR ? IS NULL OR j.attempted_at IS NULL OR j.attempted_at<?)"


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Logo limit must be between 1 and 500")


def _batch_deadline(limit):
    return time.monotonic() + min(120, 6 * limit)


def _refresh_post(db, post_id, *, deadline, fetch, image_module, retry_before=None,
                  work=None, plan=None, expected_version=None):
    started = time.time()
    with db:
        db.execute("BEGIN IMMEDIATE")
        post = db.execute("SELECT document,fingerprint FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post or (work and post[1] != expected_version):
            return None
        serialized, fingerprint = post
        try:
            doc = json.loads(serialized)
        except ValueError:
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        version = document_version(doc)
        row = db.execute("SELECT document_version FROM job_company_logos WHERE post_id=?", (post_id,)).fetchone()
        if not row or version != row[0]:
            enqueue_logo(db, post_id, doc, fingerprint)
            if not work:
                return None
        due = "" if work else f"AND j.outcome='pending' AND {_RETRY_ELIGIBLE}"
        args = () if work else (retry_before, retry_before)
        claim = db.execute(f"""UPDATE job_company_logos AS j SET attempted_at=?,lease_until=?
            WHERE post_id=? AND lease_until<=? {due} RETURNING post_id""",
            (started, started + 15, post_id, started, *args)).fetchone()
        if not claim:
            return None
    outcome, candidate, png, token = _attempt(db, plan or _plan(doc), fetch, deadline, image_module, doc)
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("""SELECT p.document, j.attempted_at FROM posts p JOIN job_company_logos j ON j.post_id=p.id
            WHERE p.id=? AND j.document_version=?""", (post_id, version)).fetchone()
        _release_image(db, token)
        if not current or tuple(current) != (serialized, started):
            return None
        if outcome == "busy":
            if work:
                db.execute("UPDATE job_company_logos SET lease_until=0 WHERE post_id=?", (post_id,))
                return None
            outcome = "pending"
        if work and plan.awaiting_official and outcome != "ready":
            # The linked response may yet supply a different, verified candidate.
            db.execute("""DELETE FROM refresh_source_snapshots WHERE work_id=? AND
                json_extract(logo_plan,'$.awaiting_official')=1""", (work["source_work_id"],))
            db.execute("UPDATE job_company_logos SET lease_until=0 WHERE post_id=?", (post_id,))
            return None
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
        if work:
            if plan.awaiting_official and outcome == "ready":
                db.execute("UPDATE job_company_logos SET identity=? WHERE post_id=?", (plan.identity, post_id))
            record_logo_receipt(db, work, {"ready": "done", "pending": "failed", "missing": "skipped"}[outcome])
    return outcome


def _refresh_batch(db, *, limit, deadline, fetch, image_module, retry_before=None):
    summary = {"checked": 0, "ready": 0, "missing": 0, "retry": 0}
    ids = db.execute(f"""SELECT j.post_id FROM job_company_logos j JOIN posts p ON p.id=j.post_id
        WHERE j.outcome='pending' AND j.lease_until<=? AND {_RETRY_ELIGIBLE}
        ORDER BY attempted_at IS NOT NULL, attempted_at, post_id LIMIT ?""", (time.time(), retry_before, retry_before, limit)).fetchall()
    for (post_id,) in ids:
        if time.monotonic() >= deadline:
            break
        outcome = _refresh_post(db, post_id, deadline=min(deadline, time.monotonic() + 5),
                                fetch=fetch, image_module=image_module, retry_before=retry_before)
        if outcome:
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


def _drain_with_signals(path, *, limit, fetch):
    invoked_at = time.time()
    directory = _signal_directory(path)
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        return drain_logos(path, limit=limit, retry_before=invoked_at, fetch=fetch)
    try:
        import fcntl

        # A systemd activation and a manual drain can target the same DB.
        fcntl.flock(fd, fcntl.LOCK_EX)
        signals = {}
        unexpected = []
        with os.scandir(fd) as entries:
            for entry in entries:
                name = _SIGNAL_NAME.fullmatch(entry.name)
                if name and entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_size == 0:
                    signals[entry.name] = int(name[1])
                elif not entry.name.startswith("."):
                    unexpected.append(entry.name)
        if not signals and unexpected:
            raise OSError(f"Unexpected entries in {directory}: {', '.join(sorted(unexpected))}")
        retry_before = max(signals.values()) / 1_000_000_000 if signals else invoked_at
        summary = drain_logos(path, limit=limit, retry_before=retry_before, fetch=fetch)
        # Keep the cutoff stable if acknowledgement is interrupted.
        for name in sorted(signals, key=signals.__getitem__):
            os.unlink(name, dir_fd=fd)
        return summary
    finally:
        os.close(fd)


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


def requeue_missing(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True)) as db, db:
        count = db.execute("""UPDATE job_company_logos SET outcome='pending',attempted_at=NULL
            WHERE outcome='missing' AND company_key IS NULL AND lease_until<=?""", (time.time(),)).rowcount
    if count:
        signal_logo_worker(path)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=DEFAULT_LOGO_LIMIT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--drain", action="store_true", help="Process all available logo requests in batches")
    mode.add_argument("--retry-missing", action="store_true", help="Queue previously missing logos and notify the worker")
    args = parser.parse_args()
    if args.retry_missing:
        summary = {"requeued": requeue_missing(args.db)}
    elif args.drain:
        summary = _drain_with_signals(args.db, limit=args.limit, fetch=fetch_public)
    else:
        summary = refresh_logos(args.db, limit=args.limit, fetch=fetch_public)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
