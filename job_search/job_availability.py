"""Conservative application deadlines; missing evidence never means closed."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
CLOSED_CATEGORY = 191700169
DEADLINE = re.compile(r"마감|(?:접수|모집|지원)\s*(?:기간|기한)|(?:서류\s*접수|입사지원)\s*[:：]|application deadline|closing date", re.I)
NOT_APPLICATION = re.compile(r"과제|면접|발표|근무|근로|수습|교육|연수|추후|미정|테스트|시험|검사|적성|응시|assessment|test|interview", re.I)
DATE = re.compile(r"(?<!\d)(?:(20\d{2})\s*(?:[./-]|년)\s*)?(\d{1,2})\s*(?:[./-]|월)\s*(\d{1,2})(?:\s*일)?(?!\d)")
CLOCK = re.compile(r"^\s*(?:\([월화수목금토일]\)|[월화수목금토일]요일)?[\s,]*(오전|오후|AM|PM)?\s*(\d{1,2})\s*(?::\s*(\d{2})|시(?:\s*(\d{1,2})\s*분)?)", re.I)
ROLLING = (("상시 채용", re.compile(r"상시\s*채용")), ("채용 시 마감", re.compile(r"채용\s*시\s*마감")))
CLOSED_TEXT = re.compile(r"^(?:(?:지원|접수|채용|모집)(?:이|가)?\s*(?:마감|종료)\s*(?:되었습니다|됐습니다)|"
                         r"(?:지원|접수|채용|모집)\s*종료|(?:채용|모집)\s*마감된 공고(?:입니다)?)[.!]?$")


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
        clock = CLOCK.match(text[last.end():])
        if clock:
            period, hour, minute, korean_minute = clock.groups()
            hour, minute = int(hour), int(minute or korean_minute or 0)
            if period:
                if not 1 <= hour <= 12:
                    return None
                hour = hour % 12 + (12 if period.upper() in {"오후", "PM"} else 0)
            if hour == 24 and minute == 0:
                at = end.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                return at.isoformat(), "datetime", "24:00"
            end = end.replace(hour=hour, minute=minute)
        return end.isoformat(), "datetime" if clock else "date", ""
    except ValueError:
        return None


def inspect_document(doc):
    lines = doc.get("lines", [])
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        lines = []
    published = _stamp(doc.get("published_gmt"))
    published = published.astimezone(KST) if published else None
    candidates, rolling = {}, {}
    for i, line in enumerate(lines):
        if NOT_APPLICATION.search(line):
            continue
        for word, pattern in ROLLING:
            if pattern.search(line):
                rolling[word] = line[:1000]
        if DEADLINE.search(line):
            context = line
            if not DATE.search(line) and i + 1 < len(lines) and not NOT_APPLICATION.search(lines[i + 1]):
                context += " " + lines[i + 1]
            deadline = _deadline(context, published)
            if deadline:
                candidates[deadline] = context[:1000]
    at, kind, word, evidence = None, "unknown", "", ""
    if len(candidates) == 1 and not rolling:
        (at, kind, word), evidence = next(iter(candidates.items()))
    elif not candidates and len(rolling) == 1:
        word, evidence = next(iter(rolling.items()))
        kind = "rolling"
    categories = doc.get("categories", [])
    closure = next((line for line in lines if CLOSED_TEXT.fullmatch(line.strip())), "")
    if isinstance(categories, list) and CLOSED_CATEGORY in categories:
        closure = "Inthiswork에서 마감 공고로 분류됨"
    return {"closed": bool(closure), "deadline_at": at, "deadline_kind": kind,
            "deadline_word": word, "evidence": closure or evidence}


def document_version(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _automatic(doc, cached):
    original = inspect_document(doc)
    if not cached:
        return original, None
    modified = _stamp(doc.get("modified_gmt"))
    cached_modified = _stamp(cached.get("source_modified_at"))
    version = cached.get("document_version")
    if version:
        if version != document_version(doc):
            return original, None
        return cached, cached.get("checked_at")
    if modified and (not cached_modified or modified > cached_modified):
        return original, None

    at = _stamp(cached.get("deadline_at"))
    published = _stamp(doc.get("published_gmt"))
    parsed = _deadline(cached.get("evidence", ""), published.astimezone(KST) if published else None)
    precision = {"deadline_kind": "date" if at else "unknown", "deadline_word": ""}
    if parsed and _stamp(parsed[0]) == at:
        precision = {"deadline_kind": parsed[1], "deadline_word": parsed[2]}
    elif modified and modified == cached_modified and (not at or _stamp(original["deadline_at"]) == at):
        precision = {key: original[key] for key in ("deadline_at", "deadline_kind", "deadline_word")}
    # Old cache timestamps determine expiry even if their display precision is unknown.
    return {**cached, **precision, "legacy_instant": bool(at and "deadline_at" not in precision)}, cached.get("checked_at")


def deadline_view(facts, now):
    kind, word = facts["deadline_kind"], facts["deadline_word"]
    at = _stamp(facts.get("deadline_at"))
    at = at.astimezone(KST) if at else None
    day = at.date().isoformat() if at else ""
    clock = at.strftime("%H:%M") if at and kind == "datetime" else ""
    if not at:
        expired = False
        label = word if kind == "rolling" else "마감일 미확인"
    elif kind == "date" and not facts.get("legacy_instant"):
        expired = now.astimezone(KST).date() > at.date()
        label = at.strftime("%m.%d 마감")
    else:
        expired = now.replace(microsecond=0) > at
        if kind == "date":
            label = at.strftime("%m.%d 마감")
        elif word == "24:00":
            label = (at - timedelta(days=1)).strftime("%m.%d 24:00 마감")
        else:
            label = at.strftime("%m.%d %H:%M 마감")
    return {"kind": kind, "label": label, "date": day, "time": clock,
            "word": word if kind == "rolling" else "", "closed": bool(facts["closed"]),
            "is_expired": bool(expired or facts["closed"])}


def availability(doc, cached=None, now=None, *, override=None):
    now = now or datetime.now(timezone.utc)
    facts, checked = _automatic(doc, cached)
    auto = deadline_view(facts, now)
    auto["version"] = document_version([auto, checked])
    manual = deadline_view({**override, "closed": bool(facts["closed"] and override["deadline_kind"] == "unknown")},
                           now) if override else None
    effective = manual or auto
    return {"is_expired": effective["is_expired"], "deadline_kind": effective["kind"],
            "deadline_label": effective["label"],
            "deadline_at": (override or facts).get("deadline_at"),
            "availability_evidence": facts["evidence"], "availability_checked_at": checked,
            "deadline_auto": auto, "deadline_manual": manual}
