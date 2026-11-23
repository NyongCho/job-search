"""Compare isolated Hermes fit sessions on the same labelled jobs and career facts."""
import argparse
import json
import os
import time
import uuid
from pathlib import Path

from . import fit_hermes
from .fit_digest import review_decision
from .fit_work import validate_entry
from .job_fit import evaluate_fit, recommendation_group, stable_hash


def example():
    facts = {"P1": "서버 개발·데이터 플랫폼 직무를 희망하고 서울 하이브리드 근무가 가능합니다.",
             "P2": "Python 배치 파이프라인을 36개월 개발하고 장애 복구 절차를 운영했습니다.",
             "P3": "PostgreSQL 실행계획을 분석하고 인덱스를 조정해 쿼리 성능을 개선했습니다.",
             "P4": "Go를 사용한 상용 API의 유지보수 및 릴리스 경험이 12개월 있습니다.",
             "P5": "업무에서 일본어로 고객과 협상한 경험은 없습니다.",
             "P6": "Kafka 클러스터는 운영하지 않았습니다. 결제 도메인 프로젝트에도 참여한 적이 없습니다.",
             "P7": "제품 API 변경 안내서와 개발자용 연동 문서를 직접 작성했습니다."}
    records = [
        ("다온｜데이터 플랫폼 엔지니어", "배치 파이프라인을 개발하고 배포 후 장애를 복구합니다.",
         ["Python 배치 개발 경력 24개월 이상이 필수입니다.", "PostgreSQL 실행계획 분석 경험이 필수입니다."],
         ["P2", "P3"], "우대 요건으로 Kafka 클러스터 운영 경험을 봅니다."),
        ("다온｜서비스 플랫폼 엔지니어", "Go API의 호환성을 관리하고 배포 후 장애를 분석합니다.",
         ["상용 Go API 유지보수 경력 36개월 이상이 필수입니다.", "PostgreSQL 쿼리 성능 개선 경험이 필수입니다."],
         ["P4", "P3"], "우대 요건으로 결제 도메인 경험을 봅니다."),
        ("보람｜데이터 서비스 개발자", "Python 배치와 데이터 조회 경로의 품질을 개선합니다.",
         ["Python 배치 개발 경력 48개월 이상이 필수입니다.", "야간 장애 복구 절차 운영 경험이 필수입니다."],
         ["P2", None], "우대 요건은 별도로 두지 않습니다."),
        ("늘봄｜일본 고객 플랫폼 담당자", "플랫폼 기술 이슈를 고객에게 설명하고 협상 과정을 담당합니다.",
         ["상용 API 유지보수 경험이 필수입니다.", "업무에서 일본어 고객 협상을 담당한 경험이 필수입니다."],
         ["P4", "P5"], "우대 요건으로 기술 문서 작성 경험을 봅니다."),
        ("해솔｜데이터 신뢰성 엔지니어", "데이터 수집 오류를 조사하고 배치 장애의 재발을 방지합니다.",
         ["PostgreSQL 실행계획 분석 경험이 필수입니다.", "금융기관의 데이터 감사 대응 경험이 필수입니다."],
         ["P3", None], "우대 요건으로 Python 배치 개발 경험을 봅니다."),
    ]
    context = [
        "채용팀 안내. 플랫폼 팀은 영업일 단위로 배포 일정을 공유하며 팀 소개 자료에는 제품과 조직의 배경도 함께 담겨 있습니다.",
        "부서 소개. 여러 제품 팀이 같은 데이터를 조회할 때 일관된 결과를 얻을 수 있도록 공통 데이터 정의를 문서로 제공합니다.",
        "업무 환경 소개. 서비스 변경을 준비할 때 팀 구성원이 함께 검토하고, 변경의 배경은 주간 회의록과 제품 이력에 남깁니다.",
        "협업 방식 안내. 신규 입사자는 담당 영역을 이해하도록 제품 담당자 및 운영 조직의 온보딩 세션에 초대됩니다.",
        "전형 안내. 제출한 자료를 검토한 뒤 직무 인터뷰와 조직 문화 대화를 진행하며 일정은 지원자와 개별적으로 협의합니다.",
        "서비스 배경. 이 팀이 관리하는 제품은 계약에 따라 서로 다른 시간대의 데이터를 제공합니다. 처리 시간과 화면의 표시 시점이 어느 지점을 기준으로 하는지 제품별 용어집과 운영 문서에서 확인할 수 있습니다.",
        "데이터 흐름 소개. 원천 시스템에서 전달되는 이벤트는 제품에 따라 건별 또는 일별로 정리됩니다. 같은 제품의 이전 자료와 현재 자료가 보이는 기준일은 각 보고서의 헤더에 표시되며, 채용 과제 데이터에는 실제 고객 식별 정보를 사용하지 않습니다.",
        "조직 소개. 개발 조직과 서비스 기획 조직은 분기별로 제품 일정과 운영 문의를 함께 검토합니다. 도입을 앞둔 기능, 고객에게 안내한 변경점, 이미 운영 중인 기능은 회의 자료에서 구분해 관리합니다.",
        "팀 업무 안내. 배포 일정이나 문의 응답 절차가 바뀔 때에는 공통 안내 문서를 갱신합니다. 회사는 내부 검토가 필요한 영향도 분석을 지원하며, 판단 과정과 최종 결정은 회의록에서 따로 확인할 수 있습니다.",
        "환경 안내. 테스트 환경에는 운영 데이터와 구별되는 예시 데이터를 사용합니다. 권한이 필요한 자료는 팀의 승인 절차에 따라 제공되므로 지원 서류에 현 직장의 비공개 제품 정보나 고객 화면을 첨부할 필요가 없습니다.",
        "면접 안내. 직무 인터뷰는 지원자가 직접 맡았던 일과 팀 차원에서 함께한 일을 구분해 설명할 수 있도록 진행합니다. 상담 가능한 일정은 지원자에게 별도 안내하며, 전형 단계별 소요 시간은 일정에 따라 달라질 수 있습니다.",
        "입사 후 안내. 온보딩 자료에는 제품의 변경 이력, 협업 조직의 역할, 사내 지원 제도의 이용 방법이 포함됩니다. 초기에는 사내 자료를 읽을 시간을 제공하며, 온보딩 계획은 입사 후 소속 조직과 협의해 확정합니다.",
        "지원 자료 안내. 자유 형식 이력서에는 담당 업무와 본인이 확인 가능한 성과를 기재해 주세요. 서류 양식이나 이전 회사의 직급 명칭은 조직마다 다르므로 실제 역할을 이해하는 데 필요한 정보만 확인합니다.",
        "결과 안내. 전형 진행 상황은 지원 시 등록한 연락 경로로 안내됩니다. 전형 과정이나 일정에 관한 질문은 채용 담당 부서가 안내하므로 개별 개발자에게 별도로 접수하실 필요는 없습니다.",
        "근무 안내. 서울 하이브리드 근무이며 고용 형태는 정규직입니다. 구체적인 세부 근무 일정은 합류 후 팀과 협의합니다.",
    ]
    jobs = []
    for i, (title, work, needs, support, preferred) in enumerate(records, 1):
        lines = [work, needs[0], *(context if i in (2, 3) else context[-1:]), needs[1], preferred]
        required = {2: support[0], len(lines) - 1: support[1]}
        jobs.append({"id": i, "title": title, "version": stable_hash([title, lines]), "lines": lines,
                     "images": [], "required": required,
                     "expected_eligibility": "eligible" if i in (1, 2) else None})
    return {"facts": facts, "jobs": jobs}


def boundary_example():
    """Include comparable Go roles just on either side of the unrounded 80-point cutoff."""
    sample = example()
    facts = sample["facts"]
    facts["P1"] = ("서울 하이브리드 데이터 파이프라인 개발 직무만 선호합니다. "
                   "Go API 운영 직무와 일본어 고객 협상 직무는 선호 밖이지만 지원 가능합니다.")
    facts["P2"] += " 배포 후 배치 장애를 직접 복구했습니다."
    facts["P4"] += " 이 기간에 Go API 호환성을 관리하고 배포 후 장애를 분석했습니다."
    job = sample["jobs"][1]
    lines = [line.replace("36개월 이상", "37개월 이상") for line in job["lines"]]
    title = "다온｜Go 플랫폼 운영 엔지니어"
    sample["jobs"][-1] = {**job, "id": 5, "title": title, "lines": lines, "version": stable_hash([title, lines])}
    return sample


def audit(jobs, entries, facts):
    rows = []
    for job in jobs:
        required = {int(k): v for k, v in job["required"].items()}
        seen, errors = set(), set()
        matching = [e for e in entries if isinstance(e, dict) and e.get("id") == job["id"]]
        score = decision = eligibility = None
        group = None
        hold_reason = ""
        criteria = []
        try:
            entry, = matching
            if entry["version"] != job["version"]:
                raise ValueError("version")
            decision = entry["decision"]
            fit = evaluate_fit(entry["assessment"], line_count=len(job["lines"]))
            score = fit["score"]
            eligibility, hold_reason = fit["eligibility"], fit["hold_reason"]
            if job.get("expected_eligibility") and eligibility != job["expected_eligibility"]:
                errors.add("지원 자격 판정")
            criteria = entry["assessment"]["criteria"]
            for criterion in criteria:
                nums, quotes = criterion["evidence_lines"], criterion["quotes"]
                if len(nums) != len(quotes) or any(not q or q not in job["lines"][n-1] for n, q in zip(nums, quotes)):
                    errors.add("JD 인용")
                evidence = set(criterion["profile_ids"])
                if evidence - facts.keys():
                    errors.add("경력 근거")
                if criterion["category"] == "required":
                    for n in set(nums) & required.keys():
                        seen.add(n)
                        if required[n] and required[n] not in evidence:
                            errors.add("필수 경력 근거")
                        if required[n] is None and criterion["status"] != "unknown":
                            errors.add("미확인 필수 경험")
            validated = validate_entry(entry, job, facts)
            validated["availability"] = "open"
            group = recommendation_group(review_decision(decision, validated), validated)
        except (KeyError, TypeError, IndexError, ValueError):
            errors.add("누락 또는 잘못된 응답")
        rows.append({"id": job["id"], "score": score, "decision": decision, "eligibility": eligibility,
                     "hold_reason": hold_reason, "group": group, "criteria": criteria,
                     "missing_required_lines": sorted(required.keys() - seen), "errors": sorted(errors)})
    return rows


def compare(single, batch):
    diffs = []
    for i in single:
        a, b = single[i], batch[i]
        numeric = a["score"] is not None and b["score"] is not None
        diffs.append({"id": i, "difference": b["score"]-a["score"] if numeric else None,
                      "crosses_80": numeric and (a["score"] >= 80) != (b["score"] >= 80),
                      "decision_changed": a["decision"] != b["decision"],
                      "group_changed": a.get("group") != b.get("group"),
                      "hold_changed": (a["score"] is None) != (b["score"] is None)})
    return diffs


def run_arm(directory, size, sample):
    directory.mkdir(mode=0o700)
    jobs, facts = sample["jobs"], sample["facts"]
    rows, attempts = [], []
    started = time.monotonic()
    first = None
    for i in range(0, len(jobs), size):
        batch = jobs[i:i + size]
        docs = [{k: j[k] for k in ("id", "title", "lines", "version", "images")} for j in batch]
        session = "fit-compare-" + uuid.uuid4().hex
        begin = time.monotonic()
        with (directory / (session + ".lock")).open("w") as lock:
            fit_hermes.run(session, facts, docs, directory, lock.fileno())
        reply = fit_hermes.recover(directory, session)
        if reply is None:
            raise RuntimeError("Hermes session has no recoverable output")
        rows.extend(audit(batch, reply["entries"], facts))
        if first is None:
            first = time.monotonic() - started
        attempts.append({**{k: v for k, v in reply.items() if k != "entries"}, "session": session,
                         "jobs": [j["id"] for j in batch], "seconds": time.monotonic() - begin})
        print(f"batch_size={size} processed={min(i+size,len(jobs))}/{len(jobs)}", flush=True)
    report = {"size": size, "seconds": time.monotonic() - started, "first_seconds": first,
              "attempts": attempts, "rows": rows}
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    fixtures = parser.add_mutually_exclusive_group()
    fixtures.add_argument("--sample", type=Path, help="Labelled {facts, jobs} fixture. Default is synthetic.")
    fixtures.add_argument("--boundary", action="store_true", help="Exercise the raw 80-point cutoff on synthetic JDs.")
    parser.add_argument("--sizes", nargs="+", type=int, choices=(1, 3, 5), default=[1, 5])
    args = parser.parse_args()
    os.umask(0o077)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    sample = json.loads(args.sample.read_text()) if args.sample else (boundary_example() if args.boundary else example())
    results = {s: run_arm(out / str(s), s, sample) for s in args.sizes}
    if 1 in results:
        indexed = {s: {r["id"]: r for r in report["rows"]} for s, report in results.items()}
        comparison = {s: compare(indexed[1], indexed[s]) for s in results if s != 1}
        (out / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
