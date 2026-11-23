"""Offline portability checks; no Hermes process, HTTP, or Slack calls."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from job_search import collect_jobs, dashboard
from tests.test_collect_jobs import posting
from tests.test_job_fit import assessment, criterion, posting_a, posting_b, posting_c

SKILL = Path(__file__).resolve().parents[1] / "skills/job-search-pipeline"


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

    def test_scored_review_matches_installed_cli_outbox_and_dashboard(self):
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        reviews = [posting_a(), posting_b(), posting_c()]
        for months in (800, 799):
            reviews.append(assessment(criterion("관련 경력", "required", "tenure", months=months,
                                                required_months=1000), preference="outside"))
        posts = []
        for index, fit in enumerate(reviews, 1):
            content = "".join(f"<p>{c['label']}</p>" for c in fit["criteria"])
            content += f'<p>지원 마감 2099-12-31</p><a href="https://example.com/job/{index}">지원하러 가기</a>'
            post = posting(index, content)
            post["title"]["rendered"] = f"익명 기업｜공고 {'ABCDE'[index-1]}"
            posts.append(post)

        source = self.base / "app.sqlite"
        collect_jobs.collect(source, lambda _: (posts, 1))
        entries = []
        for doc, fit in zip(collect_jobs.get_posts(source, range(1, 6)), reviews):
            entries.append({"id": doc["id"], "fingerprint": doc["fingerprint"], "assessment": fit,
                            "decision": "needs_review" if fit.get("question") else "recommend",
                            "reason": "업무 수행 경력 대조", "evidence_lines": [1], "availability": "open",
                            "apply_url": doc["apply_url"], "deadline": "2099-12-31", "required_summary": "관련 경력"})
        mixed_file, question_file = self.base / "mixed.json", self.base / "question.json"
        mixed_file.write_text(json.dumps(entries[::-1]))
        question_file.write_text(json.dumps([entries[2]]))
        with closing(sqlite3.connect(source)) as db:
            collected = db.execute("SELECT id, document, fingerprint FROM posts").fetchall()
        portable = self.workspace / "data/jobs.sqlite"
        bodies = []
        for name, module, workdir, jobs_db in (("portable", "collect_jobs", self.workspace, portable),
                                              ("app", "job_search.collect_jobs", SKILL.parents[1], source)):
            with self.subTest(runtime=name):
                def run(*args):
                    result = subprocess.run([sys.executable, *args], cwd=workdir, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return result.stdout.rstrip("\n")

                def cli(*args):
                    return run("-m", module, "--db", str(jobs_db), *args)

                def retry(failed, retry_id, delivered=None):
                    outcomes = {failed: "failed"}
                    if delivered:
                        outcomes[delivered] = "delivered"
                    output = run("-c", f"from {module} import reconcile_outbox; import json, sys; "
                                 "print(json.dumps(reconcile_outbox(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3])))",
                                 str(jobs_db), json.dumps(outcomes), retry_id)
                    return json.loads(output)

                if name == "portable":
                    cli("status")
                    with closing(sqlite3.connect(jobs_db)) as db, db:
                        db.executemany("INSERT INTO posts(id,document,fingerprint) VALUES(?,?,?)", collected)
                digest = cli("review", str(mixed_file), "--execution-id", "mixed")
                for score in ("86.7점", "70.0점", "80.0점"):
                    self.assertIn(score, digest)
                self.assertEqual([p for p in digest.splitlines() if p.startswith("익명 기업｜")],
                                 ["익명 기업｜공고 A · 86.7점", "익명 기업｜공고 B · 70.0점",
                                  "익명 기업｜공고 D · 80.0점", "익명 기업｜공고 C"])
                question = entries[2]["assessment"]["question"]
                self.assertIn(question, digest)
                self.assertEqual(retry("mixed", "mixed-retry"), [digest])
                question_args = ("review", str(question_file), "--execution-id", "question")
                question_digest = cli(*question_args)
                self.assertIn(question, question_digest)
                self.assertEqual(cli(*question_args), question_digest)
                self.assertEqual(retry("question", "question-retry", delivered="mixed-retry"), [question_digest])
                with closing(sqlite3.connect(jobs_db)) as db:
                    saved = db.execute("SELECT digest,delivered FROM outbox ORDER BY rowid").fetchall()
                self.assertEqual(saved, [(digest, 1), (question_digest, 0)])
                docs = json.loads(cli("show", "1", "2", "3", "4", "5"))
                scores = [doc["fit_review"]["score"] for doc in docs]
                self.assertAlmostEqual(scores[0], 86.6666666667)
                self.assertEqual(scores[1:], [70, None, 80, 79.9])
                bodies.append((digest, question_digest))

                app = dashboard.create_app({"TESTING": True, "JOBS_DB": jobs_db, "STATE_DB": self.base / f"{name}-state.sqlite",
                                            "BASE_URL": "http://localhost", "DEV_MODE": True, "SECRET_KEY": "s" * 32})
                client = app.test_client()
                listing = client.get("/").get_data(as_text=True)
                titles = [f"공고 {letter}" for letter in "ABDC"]
                positions = [listing.index(title) for title in titles]
                self.assertEqual(positions, sorted(positions))
                self.assertNotIn("공고 E", listing)
                self.assertIn(question, listing)
                detail = client.get("/jobs/1").get_data(as_text=True).split('<article id="job-detail"', 1)[1]
                for text in ("86.7점", "관련 경력", "사용자가 미경험 확인"):
                    self.assertIn(text, detail)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(*bodies)

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

    def test_installed_scheduled_path_reuses_refresh_and_creates_one_delivery(self):
        self.assertEqual(self.prepare().returncode, 0)
        source = self.base / "source.sqlite"
        collect_jobs.collect(source,lambda _: ([posting(1,'<p>배포</p><p>지원 마감 2099-01-01</p><a href="https://employer.example/1">지원하러 가기</a>')],1))
        portable = self.workspace / "data/jobs.sqlite"
        portable.parent.mkdir()
        shutil.copyfile(source,portable)
        scenario = """
from fit_work import refresh_fit_tasks,scheduled_review
from pathlib import Path
import sqlite3
db=Path("data/jobs.sqlite")
state=db.with_name("state.sqlite")
def evaluate(attempt,profile,docs):
    doc,=docs
    return {"calls":1,"entries":[{"id":1,"version":doc["version"],"decision":"recommend","reason":"운영 경험",
             "evidence_lines":[1],"required_summary":"배포","assessment":{"version":1,"complete":True,
             "eligibility":"eligible","preference":"preferred","criteria":[{"key":"deploy","label":"배포",
             "category":"work","status":"met","evidence":"배포 담당","evidence_lines":[1],
             "profile_ids":["P1"],"quotes":["배포"]}]}}]}
profile={"P1":"배포 담당"}
refresh_fit_tasks(db,state,[{"id":1,"post_id":1}],lambda *args:None,profile=profile,runner=evaluate)
def duplicate(*args): raise AssertionError("duplicate model call")
assert "100.0점" in scheduled_review(db,state,"scheduled",profile=profile,runner=duplicate)
assert scheduled_review(db,state,"next",profile=profile,runner=duplicate)=="[SILENT]"
with sqlite3.connect(db) as conn:
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0]==1
"""
        result = subprocess.run([sys.executable,"-c",scenario],cwd=self.workspace,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

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
