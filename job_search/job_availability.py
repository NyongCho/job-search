"""Conservative application deadlines; missing evidence never means closed."""
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
CLOSED_CATEGORY = 191700169
DEADLINE = re.compile(r"마감|(?:접수|모집|지원)\s*기간|입사지원\s*[:：]|application deadline|closing date", re.I)
NOT_APPLICATION = re.compile(r"과제|면접|발표|근무|근로|수습|교육|연수|추후|미정|테스트|시험|검사|적성|응시|assessment|test|interview", re.I)
DATE = re.compile(r"(?<!\d)(?:(20\d{2})\s*(?:[./-]|년)\s*)?(\d{1,2})\s*(?:[./-]|월)\s*(\d{1,2})(?:\s*일)?(?!\d)")
CLOCK = re.compile(r"(오전|오후|AM|PM)?\s*(\d{1,2})\s*(?::\s*(\d{2})|시(?:\s*(\d{1,2})\s*분)?)", re.I)


def _stamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None


def _deadline(text, published):
    matches = list(DATE.finditer(text))
    if not matches:
        return None
    if len(matches) > 1 and not re.search(r"~|～|–|부터", text):
        return None
    last = matches[-1]
    year, month, day = last.groups()
    if year is None:
        if published is None:
            return None
        year = published.year + (published.month == 12 and int(month) <= 2)
    try:
        end = datetime(int(year), int(month), int(day), 23, 59, 59, tzinfo=KST)
        if last.group(1) is None and end.date() < published.date() - timedelta(days=1):
            return None
        clock = CLOCK.search(text[last.end():])
        if clock:
            period, hour, minute, korean_minute = clock.groups()
            hour, minute = int(hour), int(minute or korean_minute or 0)
            if period:
                if not 1 <= hour <= 12:
                    return None
                hour = hour % 12 + (12 if period.upper() in {"오후", "PM"} else 0)
            if hour == 24 and minute == 0:
                return (end.replace(hour=0, minute=0, second=0) + timedelta(days=1)).isoformat()
            end = end.replace(hour=hour, minute=minute)
        return end.isoformat()
    except ValueError:
        return None


def inspect_document(doc):
    lines = doc.get("lines", [])
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        lines = []
    published = _stamp(doc.get("published_gmt"))
    published = published.astimezone(KST) if published else None
    candidates = {}
    for i, line in enumerate(lines):
        if not DEADLINE.search(line) or NOT_APPLICATION.search(line):
            continue
        context = line
        if not DATE.search(line) and i + 1 < len(lines) and not NOT_APPLICATION.search(lines[i + 1]):
            context += " " + lines[i + 1]
        deadline = _deadline(context, published)
        if deadline:
            candidates[deadline] = context[:1000]
    deadline = next(iter(candidates)) if len(candidates) == 1 else None
    categories = doc.get("categories", [])
    closed = isinstance(categories, list) and CLOSED_CATEGORY in categories
    return {"closed": closed, "deadline_at": deadline,
            "evidence": "Inthiswork에서 마감 공고로 분류됨" if closed else candidates.get(deadline, "")}


def availability(doc, cached=None, now=None):
    now = now or datetime.now(timezone.utc)
    modified = _stamp(doc.get("modified_gmt"))
    cached_modified = _stamp(cached.get("source_modified_at")) if cached else None
    if cached and modified and (not cached_modified or modified > cached_modified):
        cached = None
    facts = cached or inspect_document(doc)
    deadline = _stamp(facts.get("deadline_at"))
    return {"is_expired": bool(facts["closed"] or (deadline and now > deadline)),
            "deadline_at": facts.get("deadline_at"), "availability_evidence": facts["evidence"],
            "availability_checked_at": cached.get("checked_at") if cached else None}
