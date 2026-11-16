"""Exercise public module commands rather than the repository's file layout."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def test_module_cli_help_and_backfill_self_check(self):
        for module in ['collect_jobs', 'refresh_job_availability', 'import_profile_applications', 'evidence']:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-m', 'job_search.' + module, '--help'],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout)
        result = subprocess.run([sys.executable, '-m', 'job_search.run_job_backfill', '--self-test'],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_database_stays_at_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'job_search', root / 'job_search', ignore=shutil.ignore_patterns('__pycache__'))
            env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
            result = subprocess.run([sys.executable, '-m', 'job_search.collect_jobs', 'status'],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'data/jobs.sqlite').is_file())
            self.assertFalse((root / 'job_search/data').exists())
