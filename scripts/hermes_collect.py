"""Hermes pre-run script; install under ~/.hermes/scripts/."""
import json
import subprocess
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit


def public_dashboard_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 or c == "\\" for c in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        parsed.port
    except ValueError:
        return None
    return value.strip().rstrip("/")


def main():
    root = Path.home() / "job_search"
    sys.path.insert(0, str(root))
    from job_search.collect_jobs import reconcile_outbox

    config = json.loads((root / "deployment.json").read_text())
    job_id = config["job_id"]
    ledger = Path.home() / ".hermes/cron/executions.db"
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,status,delivery_outcome FROM executions WHERE job_id=?", (job_id,)).fetchall()
    current = [row[0] for row in rows if row[1] in {"claimed", "running"}]
    if len(current) != 1:
        raise RuntimeError("Expected exactly one active Hermes execution for this job")
    # An error notification is not an acknowledgement of the saved recommendation.
    outcomes = {row[0]: row[2] for row in rows if row[1] == "completed"}
    retry = reconcile_outbox(root / "data/jobs.sqlite", outcomes, current[0])
    payload = {"execution_id": current[0], "retry_digests": retry}
    dashboard_url = public_dashboard_url(config.get("dashboard_url"))
    if dashboard_url:
        payload["dashboard_url"] = dashboard_url
    if not retry:
        result = subprocess.run(["/usr/bin/python3", "-m", "job_search.collect_jobs", "collect"],
                                cwd=root, capture_output=True, text=True, timeout=240, check=True)
        payload.update(json.loads(result.stdout))
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
