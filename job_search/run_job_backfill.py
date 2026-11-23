"""Drain the fixed 14-day manifest through the existing Hermes job."""

import fcntl
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import collect_jobs

ROOT = Path(__file__).resolve().parents[1]

def configured_job_id():
    job_id = json.loads((ROOT / "deployment.json").read_text()).get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise RuntimeError("Configure deployment.json job_id before running the Hermes backfill")
    return job_id


def latest_execution(ledger):
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT id,status,delivery_outcome,error FROM executions WHERE job_id=? ORDER BY claimed_at DESC,id DESC LIMIT 1", (configured_job_id(),)).fetchone()
        return dict(row) if row else None


def verify_execution(result, execution, previous_id):
    # The execution ledger is authoritative; CLI status and job summaries can differ.
    if not execution or execution["id"] == previous_id or execution["status"] != "completed":
        detail = (execution or {}).get("error") or result.stderr[-500:] or result.stdout[-500:]
        raise RuntimeError(f"Hermes execution not completed (CLI exit {result.returncode}, execution={execution}): {detail}")


def verify_completion_notice(execution):
    if not execution or execution.get("status") != "completed" or execution.get("delivery_outcome") != "delivered":
        raise RuntimeError(f"Completion notice delivery not confirmed: {execution}")


def snapshot(db_path, target_ids):
    with sqlite3.connect(db_path) as db:
        rows = dict(db.execute("SELECT id,decision FROM posts"))
    missing = set(target_ids) - rows.keys()
    if missing:
        raise ValueError(f"Manifest jobs absent from database: {sorted(missing)}")
    counts = {key: 0 for key in ("pending", "recommend", "exclude", "needs_review")}
    pending = []
    for post_id in sorted(target_ids):
        decision = rows[post_id] or "pending"
        counts[decision] += 1
        if decision == "pending":
            pending.append(post_id)
    return counts, pending


def main():
    job_id = configured_job_id()
    sys.path.insert(0, str(Path.home() / ".hermes/hermes-agent"))
    from cron.jobs import get_job, update_job

    db_path = ROOT / "data/jobs.sqlite"
    manifest = json.loads((ROOT / "data/backfill-14days.json").read_text())
    target_ids = set(manifest["ids"])
    base_prompt = (ROOT / "hermes_job_prompt.txt").read_text()
    ledger = Path.home() / ".hermes/cron/executions.db"
    hermes = str(Path.home() / ".local/bin/hermes")
    state_path = ROOT / "data/backfill-status.json"
    runs = 0

    def save(state, **extra):
        counts, _ = snapshot(db_path, target_ids)
        payload = {"state": state, "updated_at": datetime.now(timezone.utc).isoformat(),
                   "range": [manifest["start_kst"], manifest["end_kst"]],
                   "total": len(target_ids), "counts": counts, "runs": runs, **extra}
        temp = state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        temp.replace(state_path)
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def wait_idle():
        while True:
            with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
                active = db.execute("SELECT count(*) FROM executions WHERE job_id=? AND status IN ('claimed','running')", (job_id,)).fetchone()[0]
            if not active:
                return
            time.sleep(15)

    def run(prompt):
        nonlocal runs
        wait_idle()
        job = get_job(job_id)
        if not job or not job.get("enabled"):
            raise RuntimeError("Job was paused/removed; respecting that change")
        update_job(job_id, {"prompt": prompt})
        previous_id = (latest_execution(ledger) or {}).get("id")
        result = subprocess.run([hermes, "cron", "run", job_id], cwd=ROOT, text=True, capture_output=True)
        runs += 1
        print(result.stdout, flush=True)
        wait_idle()
        execution = latest_execution(ledger)
        print(json.dumps({"cli_exit_code": result.returncode, "execution": execution}), flush=True)
        verify_execution(result, execution, previous_id)
        return execution

    def unacknowledged():
        with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
            outcomes = dict(db.execute("SELECT id,delivery_outcome FROM executions WHERE job_id=? AND status='completed'", (job_id,)))
        with sqlite3.connect(db_path) as db:
            rows = db.execute("SELECT id,attempts FROM outbox WHERE delivered=0").fetchall()
            remaining = 0
            for outbox_id, attempts in rows:
                if any(outcomes.get(attempt) == "delivered" for attempt in json.loads(attempts)):
                    db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (outbox_id,))
                else:
                    remaining += 1
            return remaining

    lock = (ROOT / "data/backfill.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stalls = 0
    try:
        save("running")
        while True:
            counts, pending = snapshot(db_path, target_ids)
            outstanding = unacknowledged()
            if not pending and not outstanding:
                break
            batch = pending[:100]
            instructions = (
                "\n이번 실행은 사용자가 요청한 최근 2주 전체 검토의 연속 배치입니다. "
                f"수집 기간: {manifest['start_kst']} ~ {manifest['end_kst']}.\n"
                f"이번 배치 ID: {json.dumps(batch)}\n"
                "위 ID만 검토하세요. pending 목록에서 임의로 몇 개만 고르지 마세요. "
                "retry_digests가 있으면 기존 복구 지시가 우선입니다. "
                "그 외에는 배치의 모든 ID에 대해 JD를 읽고 recommend/exclude/needs_review를 기록한 뒤 종료하세요. "
                "도중에 추천을 찾았어도 나머지를 생략하지 마세요. 모든 항목은 이번 실행의 JSON 한 파일로 모아 review를 한 번 호출하세요. "
                "이미지 위주 공고에서 실제 JD를 확보할 수 없으면 needs_review로 남기세요. "
                "원문의 마감일이 현재 한국시간보다 지났거나 마감 표시가 있으면 추천하지 마세요. "
                "마감일이 없으면 공식 채용 페이지의 실제 모집/지원 가능 상태를 확인해야 추천할 수 있습니다. "
                "열린 지원 페이지나 미래 마감일을 확인하지 못한 공고는 needs_review로 보류하세요. "
                "이번 실행에 설정된 기본 프롬프트의 결과 보관 및 최종 전달 지시를 따르세요. 이번 배치에 전체 완료 알림을 추가하지 마세요.\n"
            )
            save("running", batch_ids=batch, delivery_retries=outstanding)
            run(base_prompt + instructions)
            _, after = snapshot(db_path, target_ids)
            after_outstanding = unacknowledged()
            progressed = len(after) < len(pending) or after_outstanding < outstanding
            stalls = 0 if progressed else stalls + 1
            save("running", last_batch_reviewed=len(pending)-len(after), delivery_retries=after_outstanding)
            if stalls >= 3:
                raise RuntimeError("No review or delivery progress in three consecutive runs")
        save("review_complete")
        final_counts, _ = snapshot(db_path, target_ids)
        summary = (
            f"최근 2주 공고 검토가 완료되었습니다.\n"
            f"기간: {manifest['start_kst']} ~ {manifest['end_kst']}\n"
            f"수집: {len(target_ids)}건 · 추천 판정: {final_counts['recommend']}건 · "
            f"제외: {final_counts['exclude']}건 · 확인 필요: {final_counts['needs_review']}건\n"
            "추천 판정 수에는 기존 검토와 선호 밖 전달 기준 미만인 공고도 포함됩니다. "
            "마감 또는 모집 여부 미확인 공고는 새 추천에서 제외했습니다. "
            "이후에는 오전 9시·오후 6시 정기 수집을 이어갑니다."
        )
        notice = run("사용자가 요청한 최근 2주 공고 검토의 완료 알림입니다. 별도 작업이나 메시지 전송 도구를 호출하지 말고 다음 내용을 그대로 최종 응답으로 반환하세요.\n\n" + summary)
        verify_completion_notice(notice)
        job = get_job(job_id)
        if job.get("last_delivery_error"):
            raise RuntimeError("Review complete but completion notice delivery failed")
    except Exception as error:
        save("error", error=str(error))
        raise
    finally:
        wait_idle()
        if get_job(job_id):
            update_job(job_id, {"prompt": base_prompt})
    save("complete")


def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "jobs.sqlite"
        db = collect_jobs.connect(path)
        db.executemany("INSERT INTO posts(id,document,fingerprint,decision) VALUES(?,?,?,?)",
                       [(1,"{}","x",None),(2,"{}","y","recommend"),(3,"{}","z",None)])
        db.commit()
        db.close()
        counts, pending = snapshot(path, {1, 2})
        assert pending == [1] and counts["recommend"] == 1 and sum(counts.values()) == 2
        try:
            snapshot(path, {99})
        except ValueError:
            pass
        else:
            raise AssertionError("Missing manifest item was silently lost")
    print("backfill manifest selection self-test passed")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
