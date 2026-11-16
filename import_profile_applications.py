"""Import confirmed applications; dry-run by default, never modify profile sources."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote

import dashboard_store as store


def read_records(profile_root, repository_url, confirmed_on):
    store._date(confirmed_on)
    if repository_url and not store.safe_url(repository_url):
        raise ValueError("올바른 프로필 저장소 URL이 필요합니다.")
    root = Path(profile_root).resolve()
    records = []
    for path in sorted((root / "applications").glob("*/00_직무와_자료상태.txt")):
        if not path.resolve().is_relative_to(root):
            raise ValueError("프로필 폴더 밖의 자료는 가져올 수 없습니다.")
        summary = path.read_text(encoding="utf-8")
        company = re.search(r"^기업·과정:\s*(.+)$", summary, re.M)
        role = re.search(r"^직무:\s*(.+)$", summary, re.M)
        if not company or not role:
            raise ValueError(f"기업/직무를 읽을 수 없습니다: {path.name}")
        key = path.parent.relative_to(root).as_posix()
        section = re.search(r"(?ms)^진행 상태[ \t]*\n(.*?)(?=^자기소개서·이력서 버전 구분|\Z)", summary)
        outcomes = []
        # Only explicit assertions in the historical status section seed results.
        # Unrecognized prose stays unknown for the user to fill in.
        for sentence in re.split(r"[.!?。\n]", section.group(1) if section else ""):
            assertion = re.fullmatch(r"(?:과거\s+|당시 대화에\s+)?서류\s*(합격|불합격)"
                r"(?:\s*(?:확정|통보|확인|기록)|\s+및\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+면접 확정 기록)?", sentence.strip())
            if assertion:
                outcomes.append(assertion.group(1))
        result = {"합격": "passed", "불합격": "failed"}.get(outcomes[0], "unknown") if len(set(outcomes)) == 1 else "unknown"
        records.append({"source_key": key, "company": company.group(1).strip(), "role": role.group(1).strip(),
                        "summary": summary, "confirmed_on": confirmed_on, "document_result": result,
                        "source_url": repository_url.rstrip("/") + "/" + quote(key, safe="/") if repository_url else None})
    if not records:
        raise ValueError("가져올 기업별 지원 자료를 찾을 수 없습니다.")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--repository-url", default="")
    parser.add_argument("--confirmed-on", required=True, help="User confirmation date, not application date")
    parser.add_argument("--jobs-db", type=Path, default=Path("data/jobs.sqlite"))
    parser.add_argument("--state-db", type=Path, default=Path("data/dashboard/dashboard.sqlite"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmed-applied", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.confirmed_applied:
        parser.error("실제 반영에는 사용자의 지원 완료 확인(--confirmed-applied)이 필요합니다.")
    records = read_records(args.profile_root, args.repository_url, args.confirmed_on)
    report = {"mode": "apply" if args.apply else "dry-run", "count": len(records),
              "companies": [r["company"] for r in records]}
    if args.apply:
        store.init_state_db(args.jobs_db, args.state_db)
        report.update(store.import_profile_jobs(args.jobs_db, args.state_db, records))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
