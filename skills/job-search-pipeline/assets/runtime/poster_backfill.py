"""점진적으로 채용 포스터를 판독하고 확보된 근거를 정규 평가 대상으로 저장합니다."""

import argparse
import json
import os
import time
from contextlib import closing, nullcontext
from pathlib import Path

from collect_jobs import DEFAULT_DB
from fit_work import _db, _facts
from poster_evidence import VERSION, poster_candidates
from poster_review import prepare_post


def select_posts(db, availability_db, *, limit, post_id=None, oldest_first=False):
    candidates = []
    now = time.time()
    if post_id is not None:
        rows = db.execute("SELECT id,document FROM posts WHERE id=?", (post_id,))
    elif oldest_first:
        # Unfinished reads must not take both scheduled slots forever.
        rows = db.execute("""SELECT p.id, p.document FROM posts p
            LEFT JOIN fit_delivery d ON d.post_id=p.id WHERE p.id>0 ORDER BY d.attempted_at,p.id""")
    else:
        rows = db.execute("SELECT id,document FROM posts WHERE id>0 ORDER BY id DESC")
    for stored_id, serialized in rows:
        try:
            doc = json.loads(serialized)
            if doc["id"] != stored_id:
                raise ValueError("공고 ID 불일치")
            evidence = doc.get("poster_evidence") or {}
            retry_due = any(im.get("retry_after", float("inf")) <= now for im in evidence.get("images", []))
            if post_id is None and evidence.get("version") == VERSION and not retry_due:
                continue
            if not poster_candidates(doc) and post_id is None:
                continue
            status = _facts(availability_db, doc)[0]
        except (AttributeError, KeyError, TypeError, ValueError):
            if post_id is not None:
                raise ValueError(f"공고 {post_id}의 저장된 본문 형식을 확인할 수 없습니다.") from None
            continue
        if status != "closed" or post_id is not None:
            candidates.append({"id": stored_id, "availability": status,
                               "status": evidence.get("status", "pending")})
    candidates.sort(key=lambda row: (row["availability"] != "open", row["status"] != "pending"))
    return candidates[:limit]


def run(jobs, state, *, limit=3, post_id=None, dry_run=False, allow_model=False):
    if not 1 <= limit <= 25 or (allow_model and post_id is None):
        raise ValueError("1~25건을 선택하세요. 모델 판독에는 공고 ID가 필요합니다.")
    state_db = closing(_db(state, "ro")) if state and Path(state).is_file() else nullcontext()
    with closing(_db(jobs, "ro")) as db, state_db as availability_db:
        selected = select_posts(db, availability_db, limit=limit, post_id=post_id)
    if post_id is not None and not selected:
        raise ValueError("공고 ID를 찾을 수 없습니다.")
    if not dry_run:
        for row in selected:
            if row["availability"] == "closed":
                row["reason"] = "접수가 마감되어 건너뛰었습니다."
                continue
            reason = prepare_post(jobs, state, row["id"], allow_model=allow_model)
            with closing(_db(jobs, "ro")) as db:
                saved = db.execute("SELECT document FROM posts WHERE id=?", (row["id"],)).fetchone()
            evidence = (json.loads(saved[0]).get("poster_evidence") or {}) if saved else {}
            row.update(status=evidence.get("status", "skipped" if reason else "source_text"),
                       reason=reason or evidence.get("reason", "저장된 원문을 사용합니다."))
    return {"dry_run": dry_run, "posts": selected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=os.environ.get("JOBS_DB", DEFAULT_DB))
    parser.add_argument("--state-db", type=Path, default=os.environ.get("DASHBOARD_STATE_DB"))
    parser.add_argument("--limit", type=int, default=3, help="미처리 공고 최대 건수 (기본 3, 최대 25)")
    parser.add_argument("--post-id", type=int, help="이미 판독한 공고도 지정 가능")
    parser.add_argument("--model", action="store_true", help="해당 공고의 불확실한 이미지 최대 한 장을 모델에 제출")
    parser.add_argument("--dry-run", action="store_true", help="대상과 상태만 조회")
    args = parser.parse_args()
    if args.model and args.post_id is None:
        parser.error("--model에는 --post-id가 필요합니다.")
    state = args.state_db or args.db.parent / "dashboard/dashboard.sqlite"
    try:
        output = run(args.db, state, limit=args.limit, post_id=args.post_id,
                     dry_run=args.dry_run, allow_model=args.model)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
