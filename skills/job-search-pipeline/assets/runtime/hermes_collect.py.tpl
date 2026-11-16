"""Hermes pre-run script; install under ~/.hermes/scripts/."""
import json
import subprocess
import sqlite3
import sys
from pathlib import Path


def main():
    root = Path(__APP_DIR_PY__)
    sys.path.insert(0, str(root))
    from collect_jobs import reconcile_outbox

    config = json.loads((root / "deployment.json").read_text())
    job_id = config.get("job_id")
    if not job_id:
        raise RuntimeError("Configure deployment.json job_id after creating the Hermes job")
    ledger = Path(config["hermes_home"]) / "cron/executions.db"
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,status,delivery_outcome FROM executions WHERE job_id=?", (job_id,)).fetchall()
    current = [row[0] for row in rows if row[1] in {"claimed", "running"}]
    if len(current) != 1:
        raise RuntimeError("Expected exactly one active Hermes execution for this job")
    # An error notification is not an acknowledgement of the saved recommendation.
    outcomes = {row[0]: row[2] for row in rows if row[1] == "completed"}
    retry = reconcile_outbox(root / "data/jobs.sqlite", outcomes, current[0])
    payload = {"execution_id": current[0], "retry_digests": retry}
    if not retry:
        result = subprocess.run([config["python"], str(root / "collect_jobs.py"), "collect"],
                                cwd=root, capture_output=True, text=True, timeout=240, check=True)
        payload.update(json.loads(result.stdout))
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
