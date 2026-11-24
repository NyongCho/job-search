"""Read the favicon declared by an official careers page after matching its posting owner."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from .job_sources import _next_data, company_key

TOSS_PAGE = "https://toss.im/career/job-detail"
WEBTOON_PAGE = "https://recruit.webtoonscorp.com/rcrt/view.do"


def is_official_job_page(url):
    return bool(url and url.partition("?")[0] in {TOSS_PAGE, WEBTOON_PAGE})


class _PageBrand(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.icons = []
        self.announcement_ids = set()
        self.job_owners = set()
        self.in_head = False
        self.in_json_ld = False
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "head":
            self.in_head = True
        elif self.in_head and tag == "link" and attrs.get("rel") == "icon" and attrs.get("href"):
            self.icons.append(attrs)
        elif tag == "input" and attrs.get("type") == "hidden" and attrs.get("name") == "annoId":
            self.announcement_ids.add(attrs.get("value"))
        elif tag == "script":
            self.in_json_ld = attrs.get("type") == "application/ld+json"

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
        elif tag == "script":
            self.in_json_ld = False

    def handle_data(self, data):
        if self.in_json_ld:
            entry = json.loads(data)
            if isinstance(entry, dict) and entry.get("@type") == "JobPosting":
                self.job_owners.add(company_key(entry["hiringOrganization"]["name"]))


def _toss_query(queries, field, job_id):
    key = ["@tossteam/iso-resource", field, job_id]
    matches = [q for q in queries if q["queryKey"] == key]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous posting data")
    return json.loads(matches[0]["state"]["data"])


def _toss_owner_matches(page, job_id, sub_id):
    queries = _next_data(page)["prefetchResult"]["dehydratedState"]["queries"]
    if sub_id:
        # The group lists several affiliates, including jobs other than the one open on this page.
        group = _toss_query(queries, "job-detail-job-group", job_id)["subPositionList"]
        if [p["company"] for p in group if str(p["id"]) == sub_id] != ["토스"]:
            return False
        selected = _toss_query(queries, "job-detail-og-sub", sub_id)
    else:
        selected = _toss_query(queries, "job-detail", job_id)["job"]
    return str(selected["id"]) == (sub_id or job_id) and selected["company"] == "토스"


def toss_icon_size(url):
    size = re.fullmatch(r"https://static\.toss\.im/tds/favicon/favicon-([1-9][0-9]{1,2})x\1\.png", url)
    return int(size[1]) if size and 16 <= int(size[1]) <= 256 else None


def _toss_icon(icons):
    choices = []
    for icon in icons:
        size = toss_icon_size(icon["href"])
        if size and icon.get("sizes") == f"{size}x{size}" and icon.get("type") == "image/png":
            choices.append((size, icon["href"]))
    return max(choices)[1] if choices else None


def official_icon(page, url, company):
    if not is_official_job_page(url):
        return None
    try:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True, max_num_fields=32)
        if any(len(values) != 1 for values in query.values()):
            return None
        params = {key: values[0] for key, values in query.items()}
        if url.partition("?")[0] == TOSS_PAGE:
            if company_key(company) not in {"토스", "toss"} or params.get("company", "토스") != "토스":
                return None
            job_id, sub_id = params["job_id"], params.get("sub_position_id")
            if any(not re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in (job_id, sub_id) if value is not None):
                return None
            if _toss_owner_matches(page, job_id, sub_id):
                return _toss_icon(_PageBrand(page).icons)
        elif company_key(company) in {"네이버웹툰", "naverwebtoon"}:
            brand = _PageBrand(page)
            job_id = params["annoId"]
            if (not re.fullmatch(r"[1-9][0-9]{0,18}", job_id) or brand.announcement_ids != {job_id}
                    or brand.job_owners != {"naverwebtoon"}):
                return None
            urls = {urljoin(url, icon["href"]) for icon in brand.icons if icon.get("type") == "image/svg+xml"}
            if len(urls) == 1:
                asset = urls.pop()
                if re.fullmatch(r"https://recruit\.webtoonscorp\.com/share/tmplat/webtoon/img/og/"
                                r"webtoon_favicon(?:_[a-z0-9]+)*\.svg", asset):
                    return asset
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        pass  # Unknown or inconsistent public page data provides no employer identity.
    return None
