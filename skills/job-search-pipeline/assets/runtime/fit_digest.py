"""Render a delivery from the assessed, eligible jobs and user questions."""

from urllib.parse import urlsplit

from job_availability import availability
from job_fit import recommendation_group
from review_routing import route_review


def review_decision(decision, fit):
    if decision == "exclude" or fit["eligibility"] == "ineligible" or fit["availability"] == "closed":
        return "exclude"
    if (fit["availability"] != "open" or fit["eligibility"] == "unknown" or fit["preference"] == "unknown"
            or not fit["complete"] or fit["score"] is None):
        return "needs_review"
    return decision


def checked_link(value):
    if not isinstance(value, str) or any(ord(c) < 32 or c.isspace() or c in "<>|\\" for c in value):
        raise ValueError("Expected an absolute HTTP(S) link")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None:
        raise ValueError("Expected an absolute HTTP(S) link")
    parsed.port  # Validate the optional port as well.
    return value


def dashboard_origin(value):
    parsed = urlsplit(checked_link(value))
    if parsed.scheme != "https" or parsed.path not in {"", "/"} or "?" in value or "#" in value:
        raise ValueError("Expected an HTTPS dashboard origin without a path, query or fragment")
    return value.rstrip("/")


def review_digest(reviews, dashboard_url, *, timing=availability):
    groups = {"preferred": [], "outside": [], "question": []}
    titles = {"preferred": "희망 직무 추천", "outside": "선호 밖 추천", "question": "확인 질문"}
    for doc, entry, decision, fit in reviews:
        group = recommendation_group(decision, fit)
        current = timing(doc)
        if current["is_expired"]:
            continue
        source_doc = {**doc, "evidence_lines": entry.get("evidence_lines", [])}
        review = route_review(source_doc, decision, fit, current, reason=entry["reason"])
        if review["route"] == "question":
            group = "question"
        elif review["route"] == "hold" or group == "question":
            continue
        if not group:
            continue
        title = " ".join(doc["title"].split())
        source = checked_link(doc["url"])
        if group == "question":
            text = "\n".join([title, *review["questions"], f"원문 {source}"])
        else:
            for field in ("deadline", "required_summary"):
                if not isinstance(entry.get(field), str) or not entry[field].strip():
                    raise ValueError(f"Missing {field} for {entry['id']}")
            if "apply_url" not in fit:
                raise ValueError(f"Missing apply_url for {entry['id']}")
            lines = ", ".join(map(str, entry["evidence_lines"]))
            text = (f'{title} · {fit["score"]:.1f}점\n'
                    f'{entry["reason"].strip()} (JD {lines}행)\n'
                    f'필수 조건 {entry["required_summary"].strip()}\n마감 {entry["deadline"].strip()}\n'
                    f'지원 {fit["apply_url"]}\n원문 {source}')
        if dashboard_url:
            text += f'\n채용함에서 보기 {dashboard_url}/jobs/{entry["id"]}'
        score = 0 if group == "question" else fit["score"]
        groups[group].append((score, fit["reviewed_at"], entry["id"], text))
    sections = []
    for group, items in groups.items():
        if items:
            sections.append(titles[group] + "\n\n" + "\n\n".join(item[3] for item in sorted(items, reverse=True)))
    if sections and dashboard_url:
        sections.append(f"채용함 전체 목록 {dashboard_url}")
    return "\n\n".join(sections) or "[SILENT]"
