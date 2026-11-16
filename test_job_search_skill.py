"""Offline portability checks; no Hermes process, HTTP, or Slack calls."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "skills/job-search-pipeline"


class PortableSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "다른 프로젝트's jobs"
        self.hermes = self.base / "custom hermes"
        self.skill = self.base / "copied skill"
        shutil.copytree(SKILL, self.skill)

    def prepare(self):
        return subprocess.run(
            [sys.executable, str(self.skill / "scripts/prepare_workspace.py"),
             str(self.workspace), "--hermes-home", str(self.hermes),
             "--job-id", "separate-job", "--slack-channel", "C_TEST_ONLY"],
            cwd=self.base, capture_output=True, text=True)

    def test_copied_skill_prepares_independent_workspace_and_preserves_existing_files(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        check = subprocess.run([sys.executable, str(self.workspace / "collect_jobs.py"), "status"],
                               cwd=self.base, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(json.loads(check.stdout), {})
        config = json.loads((self.workspace / "deployment.json").read_text())
        self.assertEqual(config["job_id"], "separate-job")
        self.assertEqual(config["hermes_home"], str(self.hermes))
        original = (self.workspace / "deployment.json").read_bytes()
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.workspace / "deployment.json").read_bytes(), original)

    def test_installed_prerun_uses_new_target_and_recovers_saved_digest_offline(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.hermes / "cron").mkdir(parents=True)
        (self.hermes / "scripts").mkdir()
        with sqlite3.connect(self.hermes / "cron/executions.db") as db:
            db.execute("CREATE TABLE executions(id,job_id,status,delivery_outcome,claimed_at,error)")
            db.executemany("INSERT INTO executions VALUES(?,?,?,?,?,?)", [
                ("prior", "separate-job", "failed", "failed", "2026-01-01", "offline fixture"),
                ("current", "separate-job", "running", None, "2026-01-02", None),
                ("unrelated", "other-job", "running", None, "2026-01-03", None)])
        subprocess.run([sys.executable, str(self.workspace / "collect_jobs.py"), "status"], check=True, capture_output=True)
        with sqlite3.connect(self.workspace / "data/jobs.sqlite") as db:
            db.execute("INSERT INTO outbox VALUES(?,?,?,0)", ("prior", "saved recommendation", '["prior"]'))
        installed = self.hermes / "scripts/hermes_collect.py"
        shutil.copyfile(self.workspace / "hermes_collect.py", installed)
        result = subprocess.run([sys.executable, str(installed)], cwd=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["execution_id"], "current")
        self.assertEqual(payload["retry_digests"], ["saved recommendation"])
        result = subprocess.run([sys.executable, "-c",
            "import run_job_backfill as b; from pathlib import Path; "
            "print(b.latest_execution(Path(b.ROOT / 'deployment.json').parent.parent / 'custom hermes/cron/executions.db')['id'])"],
            cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "current")

    def backfill_fixture(self, fail_restore=False):
        self.assertEqual(self.prepare().returncode, 0)
        cron = self.hermes / "hermes-agent/cron"
        cron.mkdir(parents=True)
        (cron / "__init__.py").write_text("")
        (cron / "jobs.py").write_text('''import json, os
from pathlib import Path
def get_job(job_id):
    p = Path(os.environ['HERMES_HOME']) / 'jobs.json'
    return json.loads(p.read_text()).get(job_id)
def update_job(job_id, patch):
    home = Path(os.environ['HERMES_HOME'])
    if (home / 'fail-restore').exists() and patch.get('prompt') == (home / 'base-prompt.txt').read_text():
        raise RuntimeError('fixture restore failure')
    p = home / 'jobs.json'
    jobs = json.loads(p.read_text()); jobs[job_id].update(patch)
    p.write_text(json.dumps(jobs))
''')
        prompt = (self.workspace / "hermes_job_prompt.txt").read_text()
        (self.hermes / "base-prompt.txt").write_text(prompt)
        (self.hermes / "jobs.json").write_text(json.dumps({"separate-job": {"enabled": True, "prompt": prompt}}))
        if fail_restore:
            (self.hermes / "fail-restore").touch()
        (self.hermes / "cron").mkdir()
        with sqlite3.connect(self.hermes / "cron/executions.db") as db:
            db.execute("CREATE TABLE executions(id,job_id,status,delivery_outcome,claimed_at,error)")
        cli = self.base / "fake-hermes"
        cli.write_text(f'#!{sys.executable}\n' + '''import os, sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(os.environ['HERMES_HOME']) / 'cron/executions.db') as db:
    db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?)', ('notice', sys.argv[-1], 'completed', 'delivered', '2026-01-01', None))
''')
        cli.chmod(0o700)
        config_path = self.workspace / "deployment.json"
        config = json.loads(config_path.read_text()); config["hermes_cli"] = str(cli)
        config_path.write_text(json.dumps(config))
        subprocess.run([sys.executable, str(self.workspace / "collect_jobs.py"), "status"], check=True, capture_output=True)
        (self.workspace / "data/backfill-14days.json").write_text(json.dumps({
            "ids": [], "start_kst": "2026-01-01T00:00:00+09:00", "end_kst": "2026-01-15T00:00:00+09:00"}))
        return subprocess.run([sys.executable, str(self.workspace / "run_job_backfill.py")],
            env={**os.environ, "HERMES_HOME": str(self.base / "unrelated profile")},
            cwd=self.base, capture_output=True, text=True)

    def test_backfill_uses_configured_profile_for_cron_and_cli(self):
        result = self.backfill_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.workspace / "data/backfill-status.json").read_text())
        self.assertEqual(state["state"], "complete")
        job = json.loads((self.hermes / "jobs.json").read_text())["separate-job"]
        self.assertEqual(job["prompt"], (self.workspace / "hermes_job_prompt.txt").read_text())
        self.assertFalse((self.base / "unrelated profile").exists())

    def test_restore_failure_cannot_leave_successful_completion_state(self):
        result = self.backfill_fixture(fail_restore=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture restore failure", result.stderr)
        state = json.loads((self.workspace / "data/backfill-status.json").read_text())
        self.assertNotEqual(state["state"], "complete")


if __name__ == "__main__":
    unittest.main()
