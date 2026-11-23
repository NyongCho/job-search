from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import os
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
        (self.new / '.revision').write_text('a' * 40 + '\n')
        _, systemctl = self.logo_units(path_state=None, worker_state=None)
        service = patch.object(deploy, 'service', side_effect=systemctl)
        service.start()
        self.addCleanup(service.stop)
        fetch = patch.object(deploy, 'fetch_main', return_value=(None, 'a' * 40))
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        (self.root / 'current').symlink_to(self.old)
        self.config = {'hermes_home': str(self.root / 'hermes'), 'job_id': 'test',
                       'base_url': 'https://jobs.example.ts.net'}
        for name in ['data/jobs.sqlite', 'data/dashboard/dashboard.sqlite']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE private_data(value TEXT)')
                db.execute("INSERT INTO private_data VALUES ('preserve')")

    def test_only_exact_deploy_sha_command_is_accepted(self):
        sha = 'a' * 40
        self.assertEqual(deploy.parse_revision('deploy ' + sha), sha)
        for command in ['deploy main', 'deploy ' + sha + '; id', 'bash', 'deploy ' + '../' * 15,
                        'deploy ' + sha + '\n', 'deploy ' + sha.upper()]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                deploy.parse_revision(command)

    def test_previous_writer_keeps_delivery_eligibility_through_rollback(self):
        from job_search import collect_jobs, fit_work
        from tests.test_collect_jobs import posting

        jobs, state = deploy.database_paths(self.root)
        collect_jobs.collect(jobs, lambda _: ([posting(1)], 1))
        fit_work.migrate(jobs, state, profile={})
        with closing(sqlite3.connect(jobs)) as db, db:
            # The previous release only writes posts. It records the decision before writing fit_review.
            for post_id in (2, 3):
                db.execute("INSERT INTO posts(id,document,fingerprint) VALUES(?, '{}', 'old')", (post_id,))
            db.execute("UPDATE posts SET decision='recommend',reason='검토',reviewed_at='old' WHERE id IN (1,2)")
        fit_work.migrate(jobs, state, profile={})
        with closing(sqlite3.connect(jobs)) as db:
            delivered = db.execute("SELECT post_id,execution_id FROM fit_delivery ORDER BY post_id").fetchall()
        self.assertEqual(delivered, [(1, 'legacy'), (2, 'legacy'), (3, None)])

    def test_regular_hermes_dispatcher_executes_current_release_from_any_directory(self):
        current = self.root / 'job_search/current/scripts'
        current.mkdir(parents=True)
        (current / 'hermes_collect.py').write_text("print('current-release-dispatched')\n")
        installed = self.root / '.hermes/scripts/hermes_collect.py'
        installed.parent.mkdir(parents=True)
        installed.write_bytes((Path(__file__).parents[1] / 'scripts/hermes_dispatch.py').read_bytes())
        self.assertFalse(installed.is_symlink())
        result = subprocess.run([sys.executable, str(installed)], cwd=self.root,
                                env=dict(os.environ, HOME=str(self.root)),
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), 'current-release-dispatched')

    def test_backup_is_consistent_and_schema_changes_are_detected(self):
        src = self.root / 'data/jobs.sqlite'
        dst = self.root / '.deploy/copy.sqlite'
        deploy.backup_database(src, dst)
        self.assertEqual(deploy.schema(src), deploy.schema(dst))
        with closing(sqlite3.connect(dst)) as db, db:
            self.assertEqual(db.execute('SELECT value FROM private_data').fetchone()[0], 'preserve')
            db.execute('ALTER TABLE private_data ADD COLUMN extra TEXT')
        self.assertNotEqual(deploy.schema(src), deploy.schema(dst))

    def test_migration_checks_copy_the_databases_selected_by_the_worker(self):
        env_file = self.root / 'dashboard-environment'
        jobs = self.root / 'shared jobs.sqlite'
        state = self.root / 'shared state.sqlite'
        for marker, path in (('jobs', jobs), ('state', state)):
            with closing(sqlite3.connect(path)) as db:
                db.execute(f'CREATE TABLE {marker}_marker(value TEXT)')
        env_file.write_text(f'JOBS_DB="{jobs}"\nDASHBOARD_STATE_DB="{state}"\n')
        config = {**self.config, 'environment_file': str(env_file)}
        with patch.object(deploy, 'check_release') as check:
            deploy.validate_startup(self.root, self.new, config)
        for marker, copy in zip(('jobs', 'state'), check.call_args.args[-2:]):
            with closing(sqlite3.connect(copy)) as db:
                self.assertIsNotNone(db.execute('SELECT 1 FROM sqlite_master WHERE name=?',
                                                (marker + '_marker',)).fetchone())

    def test_refresh_service_quiesces_before_backup_and_resumes_only_on_supported_release(self):
        unit = 'job-refresh-worker.service'
        for release in (self.old, self.new):
            (release / 'deploy').mkdir()
            (release / 'deploy' / unit).touch()
        refresh = {'state': 'active'}
        previous_service = deploy.service

        def systemctl(action, name):
            if name != unit:
                return previous_service(action, name)
            if action == 'load-state':
                return 'loaded'
            if action in ('is-active', 'active-state'):
                return refresh['state']
            refresh['state'] = {'stop': 'inactive', 'start': 'active'}[action]

        records = []
        def migrate(release, jobs, state, config, *, schema_only=False):
            self.assertEqual(refresh['state'], 'inactive')
            records.append((jobs, schema_only))

        with patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'hermes', return_value={'enabled': False}), patch.object(deploy, 'healthy', return_value=True), \
             patch.object(deploy, 'validate_startup'), patch.object(deploy, 'refresh_prompt'), \
             patch.object(deploy, 'migrate_refresh_databases', side_effect=migrate):
            deploy.activate(self.root, self.new, self.config, migrate_refresh=True)
        self.assertEqual(records, [(deploy.database_paths(self.root)[0], False)])
        self.assertEqual(refresh['state'], 'active')

    def test_migration_seeds_existing_fit_using_worker_profile_settings(self):
        envfile = self.root / 'environment'
        envfile.write_text('FIT_PROFILE_FILES=/private/career.md\nHERMES_HOME=/private/hermes\n')
        jobs, state = deploy.database_paths(self.root)
        with patch.object(deploy, 'run') as run:
            deploy.migrate_refresh_databases(self.new, jobs, state, dict(self.config, environment_file=envfile))
        env = run.call_args.kwargs['env']
        self.assertEqual(env['FIT_PROFILE_FILES'], '/private/career.md')
        self.assertEqual(env['HERMES_HOME'], '/private/hermes')

    def test_prompt_cutover_recovers_exact_private_file_and_registered_job_after_interruption(self):
        from scripts.refresh_prompt import change
        directory = self.root / ".deploy/private"
        directory.mkdir()
        file = self.root / "hermes_job_prompt.txt"
        file.write_text("private file\n")
        template = directory / "new"
        template.write_text("delivery only\n")
        backup = directory / "saved.json"
        active = {"prompt": "different active instructions"}
        def update(prompt):
            active["prompt"] = prompt
        change("apply", file, backup, template, lambda:active["prompt"], update)
        self.assertEqual(file.read_text(), "delivery only\n")
        self.assertEqual(active["prompt"], "delivery only\n")
        change("apply", file, backup, template, lambda:active["prompt"], update)
        change("restore", file, backup, template, lambda:active["prompt"], update)
        self.assertEqual(active["prompt"], "different active instructions")
        self.assertEqual(file.read_text(), "private file\n")

    def test_cutover_detects_a_live_model_child_after_parent_exit(self):
        import fcntl
        jobs, _ = deploy.database_paths(self.root)
        spool = Path(str(jobs) + ".fit-runs")
        spool.mkdir()
        with (spool/"slot-0").open("a") as child_slot:
            fcntl.flock(child_slot, fcntl.LOCK_EX)
            with self.assertRaisesRegex(TimeoutError, "fit"):
                deploy.wait_fit_writers(jobs, seconds=0)
        deploy.wait_fit_writers(jobs, seconds=0)

    def test_busy_ledger_checks_only_target_job_and_requires_ledger(self):
        ledger = self.root / 'hermes/cron/executions.db'
        ledger.parent.mkdir(parents=True)
        with self.assertRaises(sqlite3.OperationalError):
            deploy.busy(self.config)
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute('CREATE TABLE executions(job_id TEXT, status TEXT)')
            db.execute("INSERT INTO executions VALUES ('other','running')")
        self.assertFalse(deploy.busy(self.config))
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute("INSERT INTO executions VALUES ('test','claimed')")
        self.assertTrue(deploy.busy(self.config))

    def run_activation(self, healthy=True, job_enabled=True):
        def hermes(action, config, reason):
            return {'enabled': job_enabled} if action == 'get' else None
        with patch.object(deploy, 'hermes', side_effect=hermes) as control, \
             patch.object(deploy, 'service', wraps=deploy.service) as service, \
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
        with closing(sqlite3.connect(self.root / 'data/jobs.sqlite')) as db, db:
            self.assertEqual(db.execute('SELECT value FROM private_data').fetchone()[0], 'preserve')

    def test_failed_health_restores_old_release_without_restoring_database(self):
        self.run_activation(healthy=False)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertFalse((self.root / '.deploy/transaction.json').exists())

    def test_prepaused_job_is_not_resumed(self):
        control, _, _ = self.run_activation(job_enabled=False)
        self.assertEqual([c.args[0] for c in control.call_args_list], ['get'])

    def test_fit_cutover_checks_copy_and_migrates_only_jobs_after_backup(self):
        jobs, state = deploy.database_paths(self.root)
        self.enable_logo_support(self.old)
        self.enable_logo_support(self.new)
        states, systemctl = self.logo_units()
        migrated = []

        def migrate(release, path):
            self.assertEqual(release, self.new)
            self.assertNotEqual(path, state)
            if path == jobs:
                self.assertEqual(states['job-logo-worker.service'], 'inactive')
                self.assertEqual(states['job-logo-worker.path'], 'inactive')
                journal = json.loads((self.root / '.deploy/transaction.json').read_text())
                self.assertEqual(journal['phase'], 'migrating_fit')
                self.assertTrue((Path(journal['backup']) / 'jobs.sqlite').is_file())
            with sqlite3.connect(path) as db:
                db.execute('ALTER TABLE private_data ADD COLUMN fit_review TEXT')
            migrated.append(path)

        with patch.object(deploy, 'migrate_fit_database', side_effect=migrate), \
             patch.object(deploy, 'check_release') as check, patch.object(deploy, 'check_previous_writer') as writer, \
             patch.object(deploy, 'hermes', return_value={'enabled': True}), patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'healthy', return_value=True):
            deploy.validate_startup(self.root, self.new, self.config, migrate_fit=True)
            self.assertEqual(check.call_args_list[0].args[1], self.old)
            self.assertEqual(writer.call_args.args[0], self.old)
            deploy.activate(self.root, self.new, self.config, migrate_fit=True)
        self.assertEqual(migrated, [self.root / '.deploy/checks/new/jobs.sqlite', jobs])
        self.assertNotEqual(deploy.schema(jobs), deploy.schema(state))
        self.assertEqual((self.root / 'current').resolve(), self.new)
        self.assertEqual(states['job-logo-worker.path'], 'active')

    def logo_units(self, *, path_state='active', worker_state='active', stop_result=None, unloaded=()):
        states = {'job-logo-worker.path': path_state, 'job-logo-worker.service': worker_state}

        def systemctl(action, unit):
            if unit not in states:
                return 'active' if action == 'is-active' else None
            if action == 'load-state':
                return 'not-found' if states[unit] is None or unit in unloaded else 'loaded'
            if action == 'is-active' and states[unit] is None:
                raise RuntimeError('Cannot determine service state')
            if action in ('is-active', 'active-state'):
                return states[unit] or 'inactive'
            if states[unit] is None:
                self.fail(f'{action} called for missing unit {unit}')
            if action == 'stop':
                if unit == 'job-logo-worker.service':
                    self.assertIn(states['job-logo-worker.path'], {'inactive', None})
                    if stop_result == 'error':
                        raise RuntimeError('cannot stop logo worker')
                    if stop_result == 'still active':
                        return
                states[unit] = 'inactive'
            if action == 'start':
                release = (self.root / 'current').resolve()
                self.assertTrue((release / 'deploy/job-logo-worker.service').is_file())
                states[unit] = 'active'

        return states, systemctl

    def enable_logo_support(self, release):
        (release / 'deploy').mkdir()
        (release / 'deploy/job-logo-worker.service').touch()

    def test_logo_worker_stops_before_live_backup_and_watcher_resumes_after_switch(self):
        self.enable_logo_support(self.old)
        self.enable_logo_support(self.new)
        states, systemctl = self.logo_units()
        marker_dir = self.root / 'data/jobs.sqlite.logo-requests'
        marker_dir.mkdir()
        marker = marker_dir / 'pending'
        marker.touch()
        backup_database = deploy.backup_database

        def backup(source, destination):
            self.assertEqual(set(states.values()), {'inactive'})
            transaction = json.loads((self.root / '.deploy/transaction.json').read_text())
            self.assertTrue(transaction['restart_logo_path'])
            backup_database(source, destination)

        with patch.object(deploy, 'hermes', return_value={'enabled': True}), \
             patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'healthy', return_value=True), \
             patch.object(deploy, 'backup_database', side_effect=backup):
            deploy.activate(self.root, self.new, self.config)

        self.assertTrue(marker.exists())
        self.assertEqual((self.root / 'current').resolve(), self.new)
        self.assertEqual(states['job-logo-worker.path'], 'active')

    def test_logo_worker_stop_failure_prevents_backup_and_switch(self):
        self.enable_logo_support(self.old)
        self.enable_logo_support(self.new)
        for result, expected in (('error', 'cannot stop logo worker'), ('still active', 'did not stop')):
            with self.subTest(result=result):
                _, systemctl = self.logo_units(stop_result=result)
                with patch.object(deploy, 'hermes', return_value={'enabled': True}), \
                     patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
                     patch.object(deploy, 'healthy', return_value=True):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        deploy.activate(self.root, self.new, self.config)

                self.assertFalse((self.root / '.deploy/backups').exists())
                self.assertEqual((self.root / 'current').resolve(), self.old)

    def test_logo_worker_started_before_its_unit_was_removed_still_stops(self):
        self.enable_logo_support(self.new)
        states, systemctl = self.logo_units(path_state='inactive', unloaded=('job-logo-worker.service',))
        with patch.object(deploy, 'hermes', return_value={'enabled': False}), \
             patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'healthy', return_value=True):
            deploy.activate(self.root, self.new, self.config)

        self.assertEqual(set(states.values()), {'inactive'})

    def test_first_logo_release_keeps_uninstalled_or_inactive_watcher_inactive(self):
        self.enable_logo_support(self.new)
        for state in (None, 'inactive'):
            with self.subTest(state=state):
                states, systemctl = self.logo_units(path_state=state, worker_state=state)
                with patch.object(deploy, 'hermes', return_value={'enabled': False}), \
                     patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
                     patch.object(deploy, 'healthy', return_value=True):
                    deploy.activate(self.root, self.new, self.config)

                self.assertEqual((self.root / 'current').resolve(), self.new)
                self.assertEqual(states['job-logo-worker.path'], state)
                deploy.switch(self.root, self.old)

    def test_failed_health_keeps_logo_watcher_stopped_on_old_release(self):
        self.enable_logo_support(self.new)
        states, systemctl = self.logo_units()
        with patch.object(deploy, 'hermes', return_value={'enabled': False}), \
             patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'healthy', side_effect=[False, True]):
            with self.assertRaisesRegex(RuntimeError, 'failed health check'):
                deploy.activate(self.root, self.new, self.config)

        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertEqual(set(states.values()), {'inactive'})

    def test_recovery_quiesces_logo_worker_with_legacy_journal_before_switching_back(self):
        self.enable_logo_support(self.new)
        deploy.switch(self.root, self.new)
        journal = self.root / '.deploy/transaction.json'
        journal.write_text(json.dumps({'previous': str(self.old), 'resume_job': False,
                                      'reason': 'test', 'restart_timer': False, 'web_stopped': True}))
        states, systemctl = self.logo_units()

        def healthy(config):
            self.assertEqual(set(states.values()), {'inactive'})
            return True

        with patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'healthy', side_effect=healthy):
            deploy.recover(self.root, self.config)

        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertFalse(journal.exists())

    def test_recovery_resumes_previously_active_watcher_after_failed_recovery(self):
        self.enable_logo_support(self.old)
        self.enable_logo_support(self.new)
        deploy.switch(self.root, self.new)
        journal = self.root / '.deploy/transaction.json'
        journal.write_text(json.dumps({'previous': str(self.old), 'resume_job': False,
                                      'reason': 'test', 'restart_timer': False, 'web_stopped': True}))
        states, systemctl = self.logo_units()
        with patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'healthy', side_effect=[False, True]):
            with self.assertRaisesRegex(RuntimeError, 'Recovery health check failed'):
                deploy.recover(self.root, self.config)
            self.assertEqual(set(states.values()), {'inactive'})
            deploy.recover(self.root, self.config)

        self.assertEqual(states['job-logo-worker.path'], 'active')
        self.assertFalse(journal.exists())

    def test_recovery_reports_missing_previously_active_watcher_on_supported_release(self):
        self.enable_logo_support(self.old)
        journal = self.root / '.deploy/transaction.json'
        journal.write_text(json.dumps({'previous': str(self.old), 'resume_job': False,
                                      'reason': 'test', 'restart_timer': False, 'web_stopped': False,
                                      'restart_logo_path': True}))
        states, systemctl = self.logo_units(path_state=None)
        with patch.object(deploy, 'service', side_effect=systemctl), patch.object(deploy, 'wait_idle'):
            with self.assertRaisesRegex(RuntimeError, 'job-logo-worker.path.*loaded'):
                deploy.recover(self.root, self.config)
            self.assertTrue(journal.exists())
            states['job-logo-worker.path'] = 'inactive'
            deploy.recover(self.root, self.config)

        self.assertEqual(states['job-logo-worker.path'], 'active')
        self.assertFalse(journal.exists())

    def test_existing_transaction_requires_explicit_recovery(self):
        transaction = self.root / '.deploy/transaction.json'
        transaction.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'recover'):
            deploy.activate(self.root, self.new, self.config)
        self.assertEqual((self.root / 'current').resolve(), self.old)

    def test_busy_timeout_restores_schedule_without_switching(self):
        with patch.object(deploy, 'hermes', return_value={'enabled': True}) as control, \
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
             patch.object(deploy, 'wait_idle'), patch.object(deploy, 'healthy', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'control unavailable'):
                deploy.activate(self.root, self.new, self.config)
        journal = json.loads((self.root / '.deploy/transaction.json').read_text())
        self.assertEqual(journal['previous'], str(self.old))

    def test_main_advancing_during_idle_wait_never_stops_web_or_switches(self):
        def finish_wait(config):
            self.fetch.return_value = (None, 'b' * 40)
        with patch.object(deploy, 'hermes', return_value={'enabled': True}) as control, \
             patch.object(deploy, 'service', wraps=deploy.service) as services, \
             patch.object(deploy, 'wait_idle', side_effect=finish_wait):
            with self.assertRaisesRegex(RuntimeError, 'Main changed'):
                deploy.activate(self.root, self.new, self.config)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertNotIn(('stop', 'job-dashboard.service'), [c.args for c in services.call_args_list])
        self.assertEqual(control.call_args_list[-1].args[0], 'resume')

    def test_failed_rollback_keeps_writers_paused_and_recovery_journal(self):
        with patch.object(deploy, 'hermes', return_value={'enabled': True}) as control, \
             patch.object(deploy, 'service', wraps=deploy.service) as services, \
             patch.object(deploy, 'wait_idle'), patch.object(deploy, 'healthy', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Rollback health failed'):
                deploy.activate(self.root, self.new, self.config)
        self.assertNotIn('resume', [c.args[0] for c in control.call_args_list])
        self.assertNotIn(('start', 'job-availability.timer'), [c.args for c in services.call_args_list])
        self.assertTrue((self.root / '.deploy/transaction.json').exists())

    def test_recovery_waits_for_workers_after_partial_schedule_restore(self):
        journal = self.root / '.deploy/transaction.json'
        journal.write_text(json.dumps({'previous': str(self.old), 'resume_job': True,
                                      'reason': 'test', 'restart_timer': True, 'web_stopped': True}))
        with patch.object(deploy, 'hermes', return_value={'enabled': True}), \
             patch.object(deploy, 'wait_idle', side_effect=TimeoutError('busy')):
            with self.assertRaises(TimeoutError):
                deploy.recover(self.root, self.config)
        self.assertTrue(journal.exists())

    def test_card_cutover_records_backup_phase_and_can_roll_back_after_first_database(self):
        journal = self.root / '.deploy/transaction.json'
        changes = []
        def migrate_database(release, path, kind):
            current = json.loads(journal.read_text())
            self.assertEqual(current['phase'], 'migrating_' + kind)
            self.assertTrue((Path(current['backup']) / 'jobs.sqlite').is_file())
            self.assertTrue((Path(current['backup']) / 'dashboard.sqlite').is_file())
            self.assertEqual((self.root / 'current').resolve(), self.old)
            changes.append(kind)
            if kind == 'state':
                raise RuntimeError('second migration failed')
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE migrated_jobs(id INTEGER)')
        with patch.object(deploy, 'migrate_database', side_effect=migrate_database), \
             patch.object(deploy, 'hermes', return_value={'enabled': True}) as hermes, \
             patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'healthy', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'second migration failed'):
                deploy.activate(self.root, self.new, self.config, migrate_cards=True)
        self.assertEqual(changes, ['jobs', 'state'])
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertFalse(journal.exists())
        self.assertEqual(hermes.call_args.args[0], 'resume')
        with closing(sqlite3.connect(self.root / 'data/jobs.sqlite')) as db, db:
            self.assertEqual(db.execute("SELECT value FROM private_data").fetchone()[0], 'preserve')
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='migrated_jobs'").fetchone())

    def test_card_cutover_failed_startup_restores_previously_disabled_services(self):
        def service(action, unit):
            if action == 'load-state':
                return 'not-found'
            if action in ('is-active', 'active-state'):
                return 'inactive'
        with patch.object(deploy, 'migrate_database'), patch.object(deploy, 'hermes', return_value={'enabled': False}), \
             patch.object(deploy, 'service', side_effect=service) as start_stop, patch.object(deploy, 'wait_idle'), \
             patch.object(deploy, 'validate_startup', side_effect=RuntimeError('candidate validation failed')):
            with self.assertRaisesRegex(RuntimeError, 'candidate validation failed'):
                deploy.activate(self.root, self.new, self.config, migrate_cards=True)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertNotIn(('restart', 'job-dashboard.service'), [c.args for c in start_stop.call_args_list])
        self.assertNotIn(('start', 'job-availability.timer'), [c.args for c in start_stop.call_args_list])


if __name__ == '__main__':
    unittest.main()
