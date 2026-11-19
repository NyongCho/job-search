"""Public source identities and the fields for a single posting."""

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from .evidence import Document

INTHISWORK_API = "https://inthiswork.com/wp-json/wp/v2/posts"
_GREENHOUSE = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_WANTED = {"wanted.co.kr", "www.wanted.co.kr"}
_ASCENT = {"ascent.me", "www.ascent.me"}


def source_name(doc):
    source = doc.get("source") or "inthiswork"
    return source.strip().casefold() if isinstance(source, str) else ""


def role_key(value):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())


def company_key(value):
    return role_key(re.sub(r"^\s*(?:㈜|주식회사|\(주\))\s*|\s*(?:㈜|주식회사|\(주\))\s*$", "", value))


def company_names(value):
    if not isinstance(value, str) or not value.strip():
        return set()
    value = re.sub(r"^\s*(?:㈜|주식회사|\(주\))\s*|\s*(?:㈜|주식회사|\(주\))\s*$", "", value)
    parts = {value, re.split(r"[（(]", value, maxsplit=1)[0], *re.findall(r"[（(]([^）)]+)[）)]", value)}
    return {key for part in parts if (key := company_key(part))}


def _id(value):
    if type(value) is int and value > 0:
        return str(value)
    return value if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) else None


def _parts(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
            return "", ""
        return parsed.hostname.lower(), parsed.path.rstrip("/")
    except (TypeError, ValueError):
        return "", ""


@dataclass(frozen=True)
class SourceLocator:
    source: str
    url: str
    original_id: str
    scope: str
    company: str
    role: str

    @property
    def endpoint(self):
        if self.source == "greenhouse":
            return f"https://boards-api.greenhouse.io/v1/boards/{self.scope}/jobs/{self.original_id}?content=true"
        if self.source == "inthiswork":
            return f"{INTHISWORK_API}/{self.original_id}"
        return self.url


def source_locator(doc):
    source, url, title = source_name(doc), doc.get("url"), doc.get("title")
    host, path = _parts(url)
    company, _, role = title.partition("｜") if isinstance(title, str) else ("", "", "")
    if source == "inthiswork":
        post_id = _id(doc.get("id"))
        if (host in {"inthiswork.com", "www.inthiswork.com"} and post_id and path == f"/archives/{post_id}"
                and company_key(company) and role_key(role)):
            return SourceLocator(source, url, post_id, host, company.strip(), role.strip())
        return None
    original_id = _id(doc.get("source_job_id"))
    if not original_id or not company_key(company) or not role_key(role):
        return None
    scope = None
    if source == "greenhouse" and host in _GREENHOUSE:
        match = re.fullmatch(r"/([\w-]+)/jobs/" + original_id, path, flags=re.ASCII)
        scope = match[1] if match else None
    elif source == "greeting" and re.fullmatch(r"[a-z0-9-]+\.career\.greetinghr\.com", host):
        scope = host.split(".")[0] if re.fullmatch(r"/[a-z]{2}/o/" + original_id, path) else None
    elif source == "wanted" and host in _WANTED and path == f"/wd/{original_id}":
        scope = "wanted.co.kr"
    elif source == "ascent" and host in _ASCENT and path == "/jobs/freepass":
        scope = "freepass"
    if scope:
        return SourceLocator(source, url, original_id, scope, company.strip(), role.strip())
    return None


def same_inthiswork_post(locator, fresh):
    match = source_locator(fresh)
    return (match and match.source == locator.source and match.original_id == locator.original_id
            and company_key(match.company) == company_key(locator.company)
            and role_key(match.role) == role_key(locator.role))


@dataclass(frozen=True)
class SourcePosting:
    lines: list
    due: str | None
    closed: bool
    logo: str | None
    organization: str
    modified: str | None = None


def _next_data(page):
    match = re.search(r"""<script\b[^>]*\bid=["']__NEXT_DATA__["'][^>]*>(.*?)</script\s*>""", page, re.S | re.I)
    if not match:
        raise ValueError("Missing public posting data")
    return json.loads(match[1])["props"]["pageProps"]


def _validate_scope(locator, url):
    check = source_locator({"source": locator.source, "source_job_id": locator.original_id,
                           "title": locator.company + "｜" + locator.role, "url": url})
    if not check or check.scope != locator.scope:
        raise ValueError("Posting URL does not match")


def _matching_job(locator, posting, company):
    if (_id(posting["id"]) != locator.original_id or role_key(posting["title"]) != role_key(locator.role)
            or not company_names(company) & company_names(locator.company)):
        raise ValueError("Posting identity does not match")


def _lines(content, url):
    return Document(content, url).lines if isinstance(content, str) and content.strip() else []


def _greenhouse(locator, final_url, body):
    fetched_host, fetched_path = _parts(final_url)
    if fetched_host != "boards-api.greenhouse.io" or fetched_path != _parts(locator.endpoint)[1]:
        raise ValueError("Greenhouse API redirected away from requested posting")
    posting = json.loads(body)
    _validate_scope(locator, posting["absolute_url"])
    _matching_job(locator, posting, posting["company_name"])
    content = posting.get("content")
    if isinstance(content, str) and content.lstrip().startswith("&lt;"):
        content = html.unescape(content)
    return SourcePosting(_lines(content, locator.url), posting.get("application_deadline"), False, None,
                         f"greenhouse:{locator.scope}", posting.get("updated_at"))


def _greeting(locator, url, page):
    _validate_scope(locator, url)
    props = _next_data(page)
    if props["host"] != locator.scope + ".career.greetinghr.com" or _id(props["openingId"]) != locator.original_id:
        raise ValueError("Greeting workspace does not match")
    matches = []
    for query in props["dehydratedState"]["queries"]:
        if query.get("queryKey", [None, None])[1:2] == ["getOpeningById"]:
            result = query["state"]["data"]
            if result.get("success") and _id(result["data"]["openingsInfo"]["openingId"]) == locator.original_id:
                matches.append(result["data"])
    if len(matches) != 1:
        raise ValueError("Greeting posting is missing or ambiguous")
    data, = matches
    posting, group = data["openingsInfo"], data["groupInfo"]
    _matching_job(locator, {**posting, "id": posting["openingId"]}, group["name"])
    status = posting["status"]
    closed = status in {"CLOSE", "CLOSED", "ENDED", "EXPIRED", "FINISHED"}
    if status != "OPEN" and not closed:
        raise ValueError("Greeting posting is unavailable")
    header = props.get("careerBootInfo", {}).get("design", {}).get("header", {})
    logo = group.get("imageUrl") or header.get("logoUrl")
    return SourcePosting(_lines(posting.get("detail"), url), posting.get("dueDate"), closed, logo,
                         f"greeting:{locator.scope}")


def _wanted(locator, url, page):
    _validate_scope(locator, url)
    posting = _next_data(page)["initialData"]
    company = posting["company"]
    _matching_job(locator, {**posting, "title": posting["position"]}, company["company_name"])
    company_id = _id(company.get("company_id"))
    status = posting["status"]
    if not company_id or posting.get("hidden") or posting.get("is_private") or status not in {"active", "closed"}:
        raise ValueError("Wanted posting is unavailable")
    lines = []
    for field in ("intro", "main_tasks", "requirements", "hire_rounds"):
        lines.extend(_lines(posting.get(field), url))
    due = posting.get("due_time")
    if not due:
        articles = re.findall(r"<article\b[^>]*\bJobDueTime_JobDueTime__\w+[^>]*>(.*?)</article\s*>", page, re.S)
        if len(articles) == 1:
            field = _lines(articles[0], url)
            if len(field) == 2 and field[0] == "마감일":
                lines.insert(0, "접수 마감: " + field[1])
    return SourcePosting(lines, due, status == "closed", company.get("logo_image"),
                         f"wanted:{company_id}")


def _ascent(locator, url, page):
    _validate_scope(locator, url)
    decoder = json.JSONDecoder()
    chunks = []
    for match in re.finditer(r"self\.__next_f\.push\(", page):
        chunk, _ = decoder.raw_decode(page, match.end())
        if isinstance(chunk, list) and len(chunk) == 2 and chunk[0] == 1 and isinstance(chunk[1], str):
            chunks.append(chunk[1])
    payload = "".join(chunks)
    matches = []
    for match in re.finditer(r'\{\s*"companies"\s*:\s*\[', payload):
        board, _ = decoder.raw_decode(payload, match.start())
        for company in board["companies"]:
            if company_key(company["name"]) == company_key(locator.company):
                matches.extend((company, position) for position in company["positions"]
                               if _id(position["id"]) == locator.original_id)
    if len(matches) != 1:
        raise ValueError("Ascent posting is missing or ambiguous")
    company, posting = matches[0]
    _matching_job(locator, posting, company["name"])
    company_id = _id(company.get("id"))
    if not company_id:
        raise ValueError("Ascent company is unavailable")
    conditions = posting.get("conditions")
    if not isinstance(conditions, list) or not all(isinstance(value, str) for value in conditions):
        raise ValueError("Ascent posting conditions are unavailable")
    return SourcePosting(conditions + _lines(posting.get("description"), url), None, False, company.get("logoUrl"),
                         f"ascent:{company_id}")


def fetch_posting(locator, fetch, deadline):
    """Read role-specific data without treating a generic or login page as a job."""
    url, body, _ = fetch(locator.endpoint, deadline=deadline, limit=2_000_000)
    parser = {"greenhouse": _greenhouse, "greeting": _greeting, "wanted": _wanted, "ascent": _ascent}[locator.source]
    try:
        return parser(locator, url, body.decode("utf-8-sig"))
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
        raise ValueError("같은 직무의 공개 공고를 확인하지 못했어요.") from error
