"""Resolve the employer-uploaded header logo of a public JOBDA posting."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .job_sources import company_names, role_key

JOBDA_DESIGN_URL = "https://api-recruiter.recruiter.co.kr/design/v2?languageType=KOR"
JOBDA_TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.recruiter\.co\.kr")
_CDN = r"https://infra1-static\.recruiter\.co\.kr/builder/"
_DATED_ID = r"[0-9]{4}/[0-9]{2}/[0-9]{2}/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
JOBDA_CONFIG_URL = re.compile(_CDN + r"recruiter/" + _DATED_ID + r"\.json")
JOBDA_IMAGE_URL = re.compile(_CDN + _DATED_ID + r"\.(?i:png|jpe?g|webp|gif)", re.ASCII)
_GENERIC = ("default", "placeholder", "jobda", "jobflex", "recruiter", "기본로고")


class _JobPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonicals = set()
        self.authors = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.add(attrs.get("href"))
        elif tag == "meta" and attrs.get("name") == "author":
            self.authors.add(attrs.get("content"))


def _design_json(url, fetch, deadline, **options):
    final, raw, _ = fetch(url, deadline=deadline, limit=256_000, **options)
    if final == url:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    return {}


def jobda_logo(page, url, company, fetch, deadline):
    location = urlsplit(url)
    host = location.hostname
    if (not host or not JOBDA_TENANT.fullmatch(host) or location.netloc != host or location.scheme != "https"
            or not re.fullmatch(r"/career/jobs/[1-9][0-9]*/?", location.path)):
        return None
    metadata = _JobPage()
    metadata.feed(page)
    names = company_names(company)
    canonical = "https://" + host + location.path.rstrip("/")
    if (metadata.canonicals != {canonical} or len(metadata.authors) != 1
            or not names & company_names(metadata.authors.pop())):
        return None
    # JOBDA's browser sends the public tenant hostname to select that employer's design.
    design = _design_json(JOBDA_DESIGN_URL, fetch, deadline, jobda_tenant=host)
    config = design.get("headFileUrl")
    if (not names & company_names(design.get("title"))
            or not isinstance(config, str) or not JOBDA_CONFIG_URL.fullmatch(config)):
        return None
    logo = _design_json(config, fetch, deadline).get("logo")
    if not isinstance(logo, dict):
        return None
    image = logo.get("logoFileUrl")
    if (not isinstance(image, str) or not JOBDA_IMAGE_URL.fullmatch(image)
            or logo.get("logoFileUid") != image.rsplit("/", 1)[-1]):
        return None
    filename = role_key(logo.get("logoFileName"))
    if not filename or any(mark in filename for mark in _GENERIC):
        return None
    # Shared job boards can upload a holding-company or group logo for their subsidiaries.
    for markers in (("holdings", "홀딩스"), ("group", "그룹")):
        if (any(marker in filename for marker in markers)
                and not any(marker in name for name in names for marker in markers)):
            return None
    return image
