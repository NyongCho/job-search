"""Score evidence-backed job reviews and route current recommendations."""

import hashlib
import json
import re
import sys
from fractions import Fraction

_WEIGHTS = {"work": 60, "required": 30, "preferred": 10}
_PRIORITY = {"required": 0, "work": 1, "preferred": 2}
_CREDIT = {"met": Fraction(1), "partial": Fraction(1, 2), "unmet": Fraction(0), "unknown": None}


def _string(value, field, *, allow_empty=False):
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise ValueError(f"Invalid fit {field}")
    return value.strip()


def _criterion(raw, line_count):
    if not isinstance(raw, dict):
        raise ValueError("Invalid fit criterion")
    item = {field: _string(raw.get(field), field) for field in ("key", "label", "category", "status")}
    category, status = item["category"], item["status"]
    if category not in _WEIGHTS or (status not in _CREDIT and status != "tenure"):
        raise ValueError("Invalid fit criterion category or status")
    lines = raw.get("evidence_lines")
    if (not isinstance(lines, list) or not lines
            or any(type(n) is not int or not 1 <= n <= line_count for n in lines)):
        raise ValueError("Invalid fit criterion evidence_lines")
    item["evidence_lines"] = sorted(set(lines))
    item["evidence"] = _string(raw.get("evidence"), "evidence", allow_empty=status == "unknown")
    if status == "tenure":
        months, required = raw.get("months"), raw.get("required_months")
        if type(months) is not int or type(required) is not int or months < 0 or required <= 0:
            raise ValueError("Invalid fit tenure months or required_months")
        item.update(months=months, required_months=required)
    elif "months" in raw or "required_months" in raw:
        raise ValueError("Fit months are only valid for tenure")
    return item


def _distinct_criteria(criteria, line_count):
    by_key = {}
    for raw in criteria:
        item = _criterion(raw, line_count)
        key = item["key"]
        previous = by_key.get(key)
        if previous is not None:
            if any(previous.get(field) != item.get(field) for field in ("status", "months", "required_months")):
                raise ValueError(f"Conflicting fit evidence for {key}")
            lines = sorted(set(previous["evidence_lines"] + item["evidence_lines"]))
            if _PRIORITY[previous["category"]] <= _PRIORITY[item["category"]]:
                item = previous
            item["evidence_lines"] = lines
        by_key[key] = item
    if any(item["status"] == "tenure" and item["category"] != "required" for item in by_key.values()):
        raise ValueError("Fit tenure must be a required criterion")
    return list(by_key.values())


def evaluate_fit(assessment: dict, *, line_count: int) -> dict:
    if (not isinstance(assessment, dict) or type(assessment.get("version")) is not int
            or assessment["version"] != 1 or type(line_count) is not int or line_count < 0):
        raise ValueError("Invalid fit assessment version or JD line count")
    complete = assessment.get("complete")
    preference = assessment.get("preference")
    eligibility = assessment.get("eligibility")
    if (type(complete) is not bool or preference not in ("preferred", "outside", "unknown")
            or eligibility not in ("eligible", "ineligible", "unknown") or not isinstance(assessment.get("criteria"), list)):
        raise ValueError("Invalid fit assessment completeness, preference, eligibility or criteria")
    criteria = _distinct_criteria(assessment["criteria"], line_count)
    raw_question = assessment.get("question")
    question = "" if raw_question is None else _string(raw_question, "question", allow_empty=True)
    owner = assessment.get("question_owner")
    if owner is not None and owner not in ("applicant", "employer", "system"):
        raise ValueError("Invalid fit question_owner")
    if len(question.splitlines()) > 1:
        raise ValueError("Fit question must be a single line")
    unknown = [item for item in criteria if item["status"] == "unknown"]
    if (complete and eligibility != "ineligible" and not question
            and owner not in ("employer", "system") and any(item["category"] == "required" for item in unknown)):
        raise ValueError("A question is required for unknown required experience")

    earned = Fraction(0)
    possible = 0
    breakdown = {}
    for category, weight in _WEIGHTS.items():
        items = [item for item in criteria if item["category"] == category]
        category_possible = weight if items or not complete else 0
        possible += category_possible
        category_earned = Fraction(0)
        known = bool(items) or complete
        for item in items:
            possible_points = Fraction(weight, len(items))
            status = item["status"]
            if status == "tenure":
                credit = min(Fraction(item["months"], item["required_months"]), 1)
            else:
                credit = _CREDIT[status]
            item["possible"] = float(possible_points)
            if credit is None:
                item["earned"] = None
                known = False
            else:
                points = possible_points * credit
                item["earned"] = float(points)
                category_earned += points
        breakdown[category] = {"earned": float(category_earned) if known else None, "possible": category_possible}
        earned += category_earned

    if eligibility == "ineligible":
        reason = "명시된 지원 제약에 해당합니다."
    elif not complete:
        reason = "공고 원문이 불완전해 요건을 확인해야 합니다."
    elif eligibility == "unknown":
        reason = "지원 가능 여부를 확인해야 합니다."
    elif unknown:
        reason = "경험 확인 필요 · " + ", ".join(item["label"] for item in unknown)
    elif not possible:
        reason = "점수를 산정할 업무와 요건이 기재되지 않았습니다."
    else:
        reason = ""

    return {"version": 1, "complete": complete, "preference": preference, "eligibility": eligibility,
            "criteria": criteria, "breakdown": breakdown, "possible": possible,
            "earned": float(earned) if complete and not unknown else None,
            "score": None if reason else float(100 * earned / possible), "hold_reason": reason,
            "question": question, **({"question_owner": owner} if owner else {})}


def recommendation_group(decision: str, fit: dict | None) -> str | None:
    if (decision not in ("recommend", "needs_review") or not fit or not fit["complete"]
            or fit.get("availability", "open") != "open" or fit["eligibility"] == "ineligible"):
        return None
    score = fit["score"]
    user_gap = (fit["preference"] == "unknown" or fit["eligibility"] == "unknown"
                or any(item["status"] == "unknown" for item in fit["criteria"]))
    if user_gap:
        return "question" if fit.get("question") else None
    if decision == "needs_review" or score is None:
        return None
    if fit["preference"] == "preferred":
        return "preferred"
    return "outside" if score >= 80 else None


def group_before_closure(fit):
    if fit and fit.get("availability") == "closed":
        return recommendation_group(fit.get("assessment_decision"), {**fit, "availability": "open"})
    return None


def read_fit(row) -> dict | None:
    """Accept only a stored v1 result for the same source and the same review."""
    try:
        fit = json.loads(row["fit_review"])
        if not isinstance(fit, dict):
            return None
        for stamp in ("fingerprint", "reviewed_at"):
            if not isinstance(fit.get(stamp), str) or not fit[stamp] or fit[stamp] != row[stamp]:
                return None
        # The writer checked JD line bounds against the fingerprinted source.
        line_count = len(json.loads(row["document"])["lines"]) if "document" in row.keys() else sys.maxsize
        expected = evaluate_fit(fit, line_count=line_count)
        if any(key not in fit or fit[key] != value for key, value in expected.items()):
            return None
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        return None
    return fit


def stable_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


# Keep the original line numbering. Only wholly administrative deadline lines are masked.
_CALENDAR_LINE = re.compile(
    r"(?:(?:지원|접수|채용|모집|서류(?:\s*접수)?)\s*)?(?:마감(?:일시|일)?|기간|기한)"
    r"\s*[:：]?\s*(?:[0-9./~\s:()년월일시오전후까지-]+|상시\s*채용|채용\s*시\s*마감)[.!]?", re.I)


def profile_jd(record, post_id):
    """Return only an explicitly sourced job description from a historical application."""
    jd = record.get("jd") or (record if record.get("jd_source") else {})
    if not isinstance(jd, dict) or not jd.get("jd_source") or not jd.get("lines"):
        jd = {"title":record.get("title",""), "url":"", "lines":[]}
    return {**jd,"id":post_id}


def jd_input(doc):
    """Only reproducible recruiting evidence belongs in the model input."""
    company = str(doc.get("title", "")).partition("｜")[0].strip()
    logo_alt = re.compile(re.escape(company) + r"\s*(?:공식\s*)?(?:로고|logo)", re.I)
    images = [im for im in doc.get("images", []) if isinstance(im, dict)
              and not (company and logo_alt.fullmatch(str(im.get("alt", "")).strip()))]
    lines = ["" if isinstance(line,str) and _CALENDAR_LINE.fullmatch(line.strip()) else line
             for line in doc.get("lines", [])]
    return {"title": doc.get("title", ""), "lines": lines, "images": images,
            "source": doc.get("jd_source") or doc.get("url", "")}


def validate_entry(entry, doc, profile):
    if entry.get("version") != doc["version"] or type(entry.get("id")) is not int:
        raise ValueError("JD 버전 불일치")
    if entry.get("decision") not in {"recommend", "needs_review", "exclude"}:
        raise ValueError("지원 판정 누락")
    if any(not isinstance(entry.get(k), str) or not entry[k].strip() for k in ("reason", "required_summary")):
        raise ValueError("판정 요약 누락")
    assessment = entry.get("assessment")
    lines = doc["lines"]
    fit = evaluate_fit(assessment, line_count=len(lines))
    evidence_lines = entry.get("evidence_lines")
    if (
        not isinstance(evidence_lines, list)
        or (not evidence_lines and entry["decision"] != "needs_review")
        or any(type(i) is not int or not 1 <= i <= len(lines) for i in evidence_lines)
    ):
        raise ValueError("JD 근거 행 오류")
    for item in assessment["criteria"]:
        refs, quotes, ids = item["evidence_lines"], item.get("quotes"), item.get("profile_ids")
        if (
            not isinstance(quotes, list)
            or len(refs) != len(quotes)
            or any(
                not isinstance(q, str) or not q.strip() or q not in lines[n - 1] for n, q in zip(refs, quotes)
            )
        ):
            raise ValueError("JD 인용문 불일치")
        if (
            not isinstance(ids, list)
            or any(not isinstance(k, str) or k not in profile for k in ids)
            or (not ids and item["status"] != "unknown")
        ):
            raise ValueError("경력 근거 불일치")
    if (
        fit["complete"]
        and fit["eligibility"] == "eligible"
        and entry["decision"] != "exclude"
        and not fit["question"]
        and fit.get("question_owner") not in {"system", "employer"}
    ):
        gaps = sorted(
            (
                item
                for item in fit["criteria"]
                if item["status"] == "unknown" and item["category"] in ("work", "preferred")
            ),
            key=lambda item: (item["category"] != "work", min(item["evidence_lines"])),
        )
        for item in gaps:
            label = " ".join(item["label"].split())
            if re.fullmatch(
                r"(?:핵심\s*)?업무|(?:우대|직무)\s*(?:요건|사항|조건)|(?:공고|원문)\s*(?:확인|접근)|"
                r"(?:(?:접수|채용|지원)\s*)?마감(?:일|시각)?|채용\s*일정|근무지|급여|고용\s*형태",
                label,
            ):
                continue
            if re.search(r"미기재|미공개|(?:원문|공고|요건)\s*(?:정보\s*)?(?:없음|확인 불가)", label):
                continue
            fit["question_owner"] = "applicant"
            fit["question"] = f"공고에 나온 ‘{label}’ 요건과 관련해 직접 수행한 업무나 프로젝트가 있나요?"
            break
    return fit


def private_answer_entry(entry, profile, doc):
    """Keep verified JD text and structured judgments from an answer-based evaluation."""
    fit = validate_entry(entry, doc, {**profile, **doc["answer_facts"]})
    assessment = {k: fit[k] for k in ("version", "complete", "preference", "eligibility")}
    criteria, labels, keys = [], [], {}
    for item in entry["assessment"]["criteria"]:
        # Quotes and profile references were checked against this JD and this job's available facts.
        source = item["quotes"][0]
        label = item["label"] if item["label"] in source else source
        key = keys.setdefault(item["key"], f"criterion-{len(keys) + 1}")
        safe = {k: item[k] for k in ("category", "status", "evidence_lines", "quotes", "profile_ids",
                                    "months", "required_months") if k in item}
        criteria.append(dict(safe, key=key, label=label, evidence="지원 근거와 대조했습니다."))
        if item["status"] == "unknown":
            labels.append(label)
    assessment["criteria"] = criteria
    owner = fit.get("question_owner")
    submitted = {fact.partition("\n")[0] for fact in doc["answer_facts"].values()}
    if fit["question"] in submitted:
        assessment.update(question=fit["question"], question_owner="applicant")
    elif fit["question"] and owner not in {"system", "employer"} and labels:
        assessment.update(question=f"공고의 ‘{labels[0]}’ 요건과 관련해 직접 수행한 경험이 있나요?",
                          question_owner="applicant")
    else:
        assessment["question_owner"] = owner or "system"
    summary = " · ".join(dict.fromkeys(c["label"] for c in criteria if c["category"] == "required"))
    return dict(id=doc["id"], version=doc["version"], decision=entry["decision"],
        reason="공고 요건과 지원자 답변을 대조했습니다.", evidence_lines=entry["evidence_lines"],
        required_summary=summary or "공고의 명시 요건을 확인했습니다.", assessment=assessment)
