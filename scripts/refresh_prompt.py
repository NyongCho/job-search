"""Switch the job's registered and on-disk prompts during an explicit refresh cutover."""
import json
import os
from pathlib import Path
import sys
import tempfile


def replace(path, content):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        tmp = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def change(action, personal, backup, template, current_prompt, set_prompt):
    if action == "restore" and not backup.exists():
        return
    personal = personal.resolve(strict=True)
    if not backup.exists():
        original = {"file": personal.read_text(), "active": current_prompt()}
        replace(backup, json.dumps(original, ensure_ascii=False))
    else:
        original = json.loads(backup.read_text())
    if action == "apply":
        desired = template.read_text()
        file_text, active_text = desired, desired
    elif action == "restore":
        file_text, active_text = original["file"], original["active"]
    else:
        raise ValueError("Unknown prompt operation")
    if personal.read_text() != file_text:
        replace(personal, file_text)
    if current_prompt() != active_text:
        set_prompt(active_text)


if __name__ == "__main__":
    from cron.jobs import get_job, update_job
    action, job_id, personal, backup, template = sys.argv[1:]
    change(action, Path(personal), Path(backup), Path(template),
           lambda: get_job(job_id)["prompt"], lambda prompt: update_job(job_id, {"prompt": prompt}))
