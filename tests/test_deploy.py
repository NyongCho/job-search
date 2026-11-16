import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('deploy', Path(__file__).parents[1] / 'scripts/deploy.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.deploy').mkdir()
        self.old = self.root / 'releases/old'
        self.new = self.root / 'releases/new'
        self.old.mkdir(parents=True)
        self.new.mkdir()
        (self.root / 'current').symlink_to(self.old)
        self.config = {'hermes_home': str(self.root / 'hermes'), 'job_id': 'test',
                       'base_url': 'https://jobs.example.ts.net'}
        for name in ['data/jobs.sqlite', 'data/dashboard/dashboard.sqlite']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE private_data(value TEXT)')
                db.execute("INSERT INTO private_data VALUES ('preserve')")

    def test_only_exact_deploy_sha_command_is_accepted(self):
        sha = 'a' * 40
        self.assertEqual(deploy.parse_revision('deploy ' + sha), sha)
        for command in ['deploy main', 'deploy ' + sha + '; id', 'bash', 'deploy ' + '../' * 15,
                        'deploy ' + sha + '\n', 'deploy ' + sha.upper()]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                deploy.parse_revision(command)

    def test_backup_is_consistent_and_schema_changes_are_detected(self):
        src = self.root / 'data/jobs.sqlite'
        dst = self.root / '.deploy/copy.sqlite'
        deploy.backup_database(src, dst)
        self.assertEqual(deploy.schema(src), deploy.schema(dst))
        with sqlite3.connect(dst) as db:
            self.assertEqual(db.execute('SELECT value FROM private_data').fetchone()[0], 'preserve')
            db.execute('ALTER TABLE private_data ADD COLUMN extra TEXT')
        self.assertNotEqual(deploy.schema(src), deploy.schema(dst))

    def test_busy_ledger_checks_only_target_job_and_requires_ledger(self):
        ledger = self.root / 'hermes/cron/executions.db'
        ledger.parent.mkdir(parents=True)
        with self.assertRaises(sqlite3.OperationalError):
            deploy.busy(self.config)
        with sqlite3.connect(ledger) as db:
            db.execute('CREATE TABLE executions(job_id TEXT, status TEXT)')
            db.execute("INSERT INTO executions VALUES ('other','running')")
        self.assertFalse(deploy.busy(self.config))
        with sqlite3.connect(ledger) as db:
            db.execute("INSERT INTO executions VALUES ('test','claimed')")
        self.assertTrue(deploy.busy(self.config))

    def run_activation(self, healthy=True, job_enabled=True):
        def hermes(action, config, reason):
            return {'enabled': job_enabled} if action == 'get' else None
        with patch.object(deploy, 'hermes', side_effect=hermes) as control, \
             patch.object(deploy, 'service', return_value='active') as service, \
             patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'healthy', side_effect=[healthy, True]) as health:
            if healthy:
                deploy.activate(self.root, self.new, self.config)
            else:
                with self.assertRaisesRegex(RuntimeError, 'health'):
                    deploy.activate(self.root, self.new, self.config)
            return control, service, health

    def test_success_switches_code_and_keeps_private_data(self):
        control, _, _ = self.run_activation()
        self.assertEqual((self.root / 'current').resolve(), self.new)
        self.assertFalse((self.root / '.deploy/transaction.json').exists())
        self.assertEqual([c.args[0] for c in control.call_args_list], ['get', 'pause', 'resume'])
        self.assertEqual(len(list((self.root / '.deploy/backups').glob('*/jobs.sqlite'))), 1)
        with sqlite3.connect(self.root / 'data/jobs.sqlite') as db:
            self.assertEqual(db.execute('SELECT value FROM private_data').fetchone()[0], 'preserve')

    def test_failed_health_restores_old_release_without_restoring_database(self):
        self.run_activation(healthy=False)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertFalse((self.root / '.deploy/transaction.json').exists())

    def test_prepaused_job_is_not_resumed(self):
        control, _, _ = self.run_activation(job_enabled=False)
        self.assertEqual([c.args[0] for c in control.call_args_list], ['get'])

    def test_existing_transaction_requires_explicit_recovery(self):
        transaction = self.root / '.deploy/transaction.json'
        transaction.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'recover'):
            deploy.activate(self.root, self.new, self.config)
        self.assertEqual((self.root / 'current').resolve(), self.old)

    def test_busy_timeout_restores_schedule_without_switching(self):
        with patch.object(deploy, 'hermes', return_value={'enabled': True}) as control, \
             patch.object(deploy, 'service', return_value='active'), \
             patch.object(deploy, 'wait_idle', side_effect=TimeoutError('busy')):
            with self.assertRaises(TimeoutError):
                deploy.activate(self.root, self.new, self.config)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertEqual([c.args[0] for c in control.call_args_list], ['get', 'pause', 'resume'])

    def test_failed_schedule_restore_retains_recovery_journal(self):
        def hermes(action, config, reason):
            if action == 'resume':
                raise RuntimeError('control unavailable')
            return {'enabled': True}
        with patch.object(deploy, 'hermes', side_effect=hermes), \
             patch.object(deploy, 'service', return_value='active'), \
             patch.object(deploy, 'wait_idle'), patch.object(deploy, 'healthy', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'control unavailable'):
                deploy.activate(self.root, self.new, self.config)
        journal = json.loads((self.root / '.deploy/transaction.json').read_text())
        self.assertEqual(journal['previous'], str(self.old))


if __name__ == '__main__':
    unittest.main()
