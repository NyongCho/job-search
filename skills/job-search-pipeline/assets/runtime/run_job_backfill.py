"""Drain the fixed 14-day manifest through the existing Hermes job."""

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import collect_jobs

ROOT = Path(__file__).resolve().parent
JOB_ID = json.loads((ROOT / "deployment.json").read_text()).get("job_id")


def latest_execution(ledger):
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT id,status,delivery_outcome,error FROM executions WHERE job_id=? ORDER BY claimed_at DESC,id DESC LIMIT 1", (JOB_ID,)).fetchone()
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
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='fit_delivery'").fetchone():
            for post_id, in db.execute("SELECT post_id FROM fit_delivery WHERE execution_id IS NULL"):
                if post_id in rows:
                    rows[post_id] = None
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


def completion_counts(counts):
    reviewed = counts["recommend"] + counts["exclude"] + counts["needs_review"]
    return f"검토 완료 {reviewed}건 · 추천 판정 {counts['recommend']}건 · 제외 판정 {counts['exclude']}건"


def main():
    configuration = json.loads((ROOT / "deployment.json").read_text())
    if not JOB_ID:
        raise RuntimeError("Configure deployment.json job_id after creating the Hermes job")
    os.environ["HERMES_HOME"] = configuration["hermes_home"]
    sys.path.insert(0, configuration["hermes_repo"])
    from cron.jobs import get_job, update_job

    db_path = ROOT / "data/jobs.sqlite"
    manifest = json.loads((ROOT / "data/backfill-14days.json").read_text())
    target_ids = set(manifest["ids"])
    base_prompt = (ROOT / "hermes_job_prompt.txt").read_text()
    ledger = Path(configuration["hermes_home"]) / "cron/executions.db"
    hermes = configuration["hermes_cli"]
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
                active = db.execute("SELECT count(*) FROM executions WHERE job_id=? AND status IN ('claimed','running')", (JOB_ID,)).fetchone()[0]
            if not active:
                return
            time.sleep(15)

    def run(prompt):
        nonlocal runs
        wait_idle()
        job = get_job(JOB_ID)
        if not job or not job.get("enabled"):
            raise RuntimeError("Job was paused/removed; respecting that change")
        update_job(JOB_ID, {"prompt": prompt})
        previous_id = (latest_execution(ledger) or {}).get("id")
        result = subprocess.run([hermes, "cron", "run", JOB_ID], cwd=ROOT, text=True, capture_output=True)
        runs += 1
        print(result.stdout, flush=True)
        wait_idle()
        execution = latest_execution(ledger)
        print(json.dumps({"cli_exit_code": result.returncode, "execution": execution}), flush=True)
        verify_execution(result, execution, previous_id)
        return execution

    def unacknowledged():
        with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
            outcomes = dict(db.execute("SELECT id,delivery_outcome FROM executions WHERE job_id=? AND status='completed'", (JOB_ID,)))
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
            instructions = ("\n이번 실행은 지정 기간 공고 검토 배치입니다. 프리런이 고정 ID의 평가를 처리했습니다. "
                            "retry_digests가 있으면 기존 전달문만, 그 외에는 scheduled_digest를 그대로 반환하세요.\n")
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
            f"지정 기간 공고 검토가 완료되었습니다.\n"
            f"기간: {manifest['start_kst']} ~ {manifest['end_kst']}\n"
            f"{completion_counts(final_counts)}\n"
            "지원자가 답할 확인 질문은 해당 공고의 알림에서 확인할 수 있습니다. "
            "추천 판정 수에는 기존 검토와 선호 밖 전달 기준 미만인 공고도 포함됩니다. "
            "마감 또는 모집 여부 미확인 공고는 새 추천에서 제외했습니다. "
            f"이후 정기 수집 설정: {configuration['schedule']} ({configuration['timezone']})."
        )
        notice = run("사용자가 요청한 지정 기간 공고 검토의 완료 알림입니다. 별도 작업이나 메시지 전송 도구를 호출하지 말고 다음 내용을 그대로 최종 응답으로 반환하세요.\n\n" + summary)
        verify_completion_notice(notice)
        job = get_job(JOB_ID)
        if job.get("last_delivery_error"):
            raise RuntimeError("Review complete but completion notice delivery failed")
    except Exception as error:
        save("error", error=str(error))
        raise
    finally:
        wait_idle()
        if get_job(JOB_ID):
            update_job(JOB_ID, {"prompt": base_prompt})
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
