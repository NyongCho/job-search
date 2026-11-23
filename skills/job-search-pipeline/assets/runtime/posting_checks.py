"""Validation shared with the collector source refresh."""
from collections import Counter
from datetime import date, datetime, time as clock
from urllib.parse import urlencode
from collect_jobs import API,FIELDS,extract
from job_availability import KST,_stamp,inspect_document
from job_sources import same_inthiswork_post


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
