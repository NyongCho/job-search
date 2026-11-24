"""Hermes pre-run script; install under ~/.hermes/scripts/."""
import json
import os
import shlex
import subprocess
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit


def worker_environment(root):
    env = os.environ.copy()
    settings = Path.home() / ".config/job-dashboard/environment"
    deploy_config = root / ".deploy/config.json"
    if deploy_config.is_file():
        settings = Path(json.loads(deploy_config.read_text()).get("environment_file", settings))
    if settings.is_file():
        keys = {"JOBS_DB", "DASHBOARD_STATE_DB", "HERMES_HOME", "HERMES_REPO", "HERMES_PYTHON",
                "FIT_PROFILE_FILES", "FIT_BATCH_SIZE", "FIT_CONCURRENCY", "FIT_INPUT_CHAR_LIMIT",
                "FIT_MODEL", "FIT_PROVIDER", "POSTER_TESSERACT", "TESSDATA_PREFIX"}
        for line in settings.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key in keys:
                parts = shlex.split(value, comments=True)
                if len(parts) > 1:
                    raise ValueError(f"Quote spaces in {key} in the dashboard environment file")
                env[key] = parts[0] if parts else ""
    env.setdefault("DASHBOARD_STATE_DB", str(root / "data/dashboard/dashboard.sqlite"))
    return env


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
    current_release = root / "current"
    release = current_release if (current_release / "job_search/collect_jobs.py").is_file() else root
    sys.path.insert(0, str(release))
    from job_search.collect_jobs import reconcile_outbox

    config = json.loads((root / "deployment.json").read_text())
    job_id = config["job_id"]
    env = worker_environment(root)
    jobs = release / env.get("JOBS_DB", root / "data/jobs.sqlite")
    scheduler_home = Path(os.environ.get("HERMES_HOME", env.get("HERMES_HOME", Path.home() / ".hermes")))
    ledger = scheduler_home / "cron/executions.db"
    with sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,status,delivery_outcome FROM executions WHERE job_id=?", (job_id,)).fetchall()
    current = [row[0] for row in rows if row[1] in {"claimed", "running"}]
    if len(current) != 1:
        raise RuntimeError("Expected exactly one active Hermes execution for this job")
    # An error notification is not an acknowledgement of the saved recommendation.
    outcomes = {row[0]: row[2] for row in rows if row[1] == "completed"}
    retry = reconcile_outbox(jobs, outcomes, current[0])
    payload = {"execution_id": current[0], "retry_digests": retry}
    dashboard_url = public_dashboard_url(config.get("dashboard_url"))
    if dashboard_url:
        payload["dashboard_url"] = dashboard_url
    if not retry:
        python = next((path for path in (current_release / ".venv-dashboard/bin/python",
                                         root / ".venv-dashboard/bin/python") if path.is_file()), sys.executable)
        command = [str(python), "-m", "job_search.collect_jobs", "--db", str(jobs), "scheduled", "--execution-id", current[0]]
        if dashboard_url:
            command.extend(["--dashboard-url", dashboard_url])
        result = subprocess.run(command,
                                cwd=release, env=env, stdout=subprocess.PIPE, text=True, timeout=3500, check=True)
        payload.update(json.loads(result.stdout))
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
