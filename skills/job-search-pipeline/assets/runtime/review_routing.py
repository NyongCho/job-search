"""Classify review work from current, stored evidence without starting an evaluation."""
import re

from job_availability import has_jd_content


PENDING = "적합도 평가 대기"
EVALUATION_ERROR = "적합도 평가 오류로 결과를 확인할 수 없어요."
OPENING = "현재 접수 여부를 확인하지 못했어요."
EMPLOYER = "기업에 확인해야 할 채용 조건이 있어요."
MISSING = "직무와 요건을 확인할 공고 본문이 없어요."
IMAGE = "채용 이미지의 직무와 요건을 아직 판독하지 못했어요."

# Untyped legacy questions need positive evidence that the applicant can answer.
# A question mark or a mention of experience alone does not establish ownership.
_EMPLOYER = re.compile(
    r"채용\s*담당|채용팀|인사팀|회사(?:가|는)|기업(?:이|은)|(?:기업|회사)\s*측|"
    r"(?:회사|기업)에(?:서)?\s*(?:확인|문의)|인정(?:하|되|해|받)|허용|제공(?:하|되)|지원\s*자격이|"
    r"필수(?:인가|입니까|인지)|요구(?:되|하)|우대(?:하|되)|경력.{0,12}받아주|고용\s*형태|"
    r"(?:포지션|직무|채용|공고)(?:인가요|입니까|인지)|회사의?\s*(?:방침|정책)|"
    r"경력.{0,16}포함(?:하|되)|\b(?:your company|the employer|is .{0,40} hiring)\b", re.I)
_SOURCE_REASONS = (
    (re.compile(r"채용\s*(?:이미지|포스터)|(?:이미지|포스터|OCR).{0,24}(?:판독|판별|미확인|확인\s*불가|읽을)", re.I), IMAGE),
    (re.compile(r"공고\s*(?:원문|상세)|원문\s*(?:링크|확인)|(?:JD|원문|본문).{0,20}(?:누락|미확보|없음|접근\s*(?:실패|불가))", re.I), MISSING),
    (re.compile(r"마감일|(?:접수|지원|채용)\s*마감|접수\s*(?:기간|여부|중|상태)|지원\s*링크|모집\s*중"), OPENING),
)
_PERSONAL = re.compile(
    r"경험|경력|직접|수행|담당|맡았|참여|사용해|사용한|보유|보유하|자격증|졸업|재학|"
    r"선호|희망|의향|의사[가를는]?\s|(?:근무|출근|입사|합류).{0,24}(?:가능하신|가능한지|할\s*수|가능하세요)|"
    r"(?:전일제|풀타임|주\s*\d일|주\s*\d시간).{0,24}(?:가능하신|가능하세요|할\s*수)|"
    r"\b(?:can|would|have|do|did|are) you (?:work|commute|relocate|have|prefer|hold|use|available|experienced)\b", re.I)
_GENERIC = re.compile(r"(?:관련\s*|필수\s*|직무\s*)?(?:업무\s*)?(?:경험|경력)(?:이|은)?\s*(?:있나요|있으신가요|있습니까)[?？]?")
_PLACEHOLDER = re.compile(
    r"(?:지원(?:하러\s*가기|하기)|apply\s*(?:now)?|(?:공고|채용)\s*(?:상세|안내|페이지|포스터)|"
    r"(?:.{0,40}(?:이미지|포스터|첨부\s*(?:파일|자료)).{0,16}(?:확인|참조|참고).{0,12})|"
    r"(?:주요\s*업무|업무\s*내용|(?:필수|우대|자격)\s*(?:사항|조건|요건))|https?://\S+)[.!]?", re.I)


def _result(route=None, reason="", question=None):
    return {"route": route, "reason": reason, "questions": [question] if question else []}


def _text_evidence(doc, numbers=None):
    lines = doc.get("lines")
    if not isinstance(lines, list):
        return False
    if numbers is not None:
        lines = [lines[n - 1] for n in numbers if type(n) is int and 1 <= n <= len(lines)]
    lines = [line for line in lines if isinstance(line, str) and line.strip()
             and line.strip() != doc.get("title") and not _PLACEHOLDER.fullmatch(line.strip())]
    return has_jd_content({"lines": lines, "links": doc.get("links") or []})


def _operational_reason(text, owner):
    for pattern, cause in _SOURCE_REASONS:
        if pattern.search(text):
            return cause
    if owner == "employer" or _EMPLOYER.search(text):
        return EMPLOYER
    return PENDING if owner == "system" else None


def _grounded_question(question, doc, criteria):
    # Older assessments lack ownership. Link their question to a concrete subject
    # in the cited JD or the corresponding, validated criterion label.
    lines = doc.get("lines", [])
    references = [*doc.get("evidence_lines", [])]
    subjects = []
    for criterion in criteria:
        references.extend(criterion.get("evidence_lines", []))
        subjects.append(criterion.get("label", ""))
    subjects.extend(lines[n - 1] for n in references if type(n) is int and 1 <= n <= len(lines))
    generic = {"경험", "경력", "관련", "필수", "우대", "요건", "자격", "업무", "직무", "직접", "수행", "지원",
               "프로젝트", "담당", "근무", "있는", "분야", "사항", "보유", "운영자",
               "experience", "required", "work", "the", "and", "with", "you", "your"}
    for term in re.findall(r"[가-힣a-z0-9+#]{2,}", " ".join(subjects).casefold()):
        term = re.sub(r"(?:으로|에서|과의|할|한|의|이|가|을|를|은|는|와|과)$", "", term)
        if len(term) >= 2 and term not in generic and term in question.casefold():
            return True
    return False


def _applicant_question(text, owner):
    if (not isinstance(text, str) or len(text) > 500 or len(text.splitlines()) != 1
            or not text.endswith(("?", "？", "나요", "니까", "신가요")) or _GENERIC.fullmatch(text)):
        return False
    return owner == "applicant" or bool(_PERSONAL.search(text))


def route_review(doc, decision, fit, availability, *, reason="", evaluation_error=False):
    """Return one question, one operational hold, or no outstanding review work."""
    if availability.get("is_expired") or (fit and fit.get("availability") == "closed"):
        return _result()
    is_open = (fit.get("availability") == "open" if fit else
               availability.get("deadline_kind") in {"date", "datetime", "rolling"})
    return _unresolved(doc, decision, fit, reason, is_open, evaluation_error)


def question_if_open(doc, decision, fit, reason):
    """A deadline check is worthwhile if confirming an opening would expose a question."""
    return _unresolved(doc, decision, fit, reason, is_open=True, evaluation_error=False)["questions"]


def _unresolved(doc, decision, fit, reason, is_open, evaluation_error):
    poster = doc.get("poster_evidence")
    if isinstance(poster, dict) and poster.get("status") in {"uncertain", "failed"}:
        explanation = poster.get("reason")
        return _result("hold", explanation if isinstance(explanation, str) and explanation.strip() else IMAGE)
    if decision == "exclude" or (fit and fit.get("eligibility") == "ineligible"):
        return _result()
    if decision == "recommend" and not fit:
        return _result()
    if not _text_evidence(doc):
        images = doc.get("images")
        has_poster = isinstance(images, list) and has_jd_content({"images": images})
        return _result("hold", IMAGE if has_poster else MISSING)
    if fit and not fit.get("complete"):
        return _result("hold", "직무와 요건을 평가할 공고 본문이 불완전해요.")
    if evaluation_error:
        return _result("hold", EVALUATION_ERROR)

    if fit:
        if decision == "recommend" and is_open and fit.get("score") is not None and fit.get("preference") != "unknown":
            return _result()
        if not is_open:
            return _result("hold", OPENING)
        question = (fit.get("question") or "").strip()
        owner = fit.get("question_owner")
        criteria = fit.get("criteria", [])
        gap = (fit.get("eligibility") == "unknown" or fit.get("preference") == "unknown"
               or any(c["status"] == "unknown" for c in criteria))
        grounded = any(_text_evidence(doc, c.get("evidence_lines", [])) for c in criteria)
        # Eligibility and preference questions may cite the review's JD lines directly.
        grounded = grounded or _text_evidence(doc, doc.get("evidence_lines", []))
    else:
        if decision != "needs_review":
            return _result("hold", PENDING)
        # Unscored reviews have only a reason and numbered JD evidence. Never generate
        # a new question from an inferred skill, job title, or apparent lack of evidence.
        question = re.split(r"(?:[.!。]\s+|\n)+", reason.strip())[-1].strip()
        owner = None
        if not question.endswith(("?", "？")):
            return _result("hold", _operational_reason(reason, owner) or PENDING)
        if not is_open:
            return _result("hold", OPENING)
        grounded = _text_evidence(doc, doc.get("evidence_lines", []))
        gap = True

    if cause := _operational_reason(question, owner):
        return _result("hold", cause)
    if (gap and grounded and _applicant_question(question, owner)
            and (owner == "applicant" or _grounded_question(question, doc, (fit or {}).get("criteria", [])))):
        return _result("question", "", question)
    return _result("hold", PENDING)
