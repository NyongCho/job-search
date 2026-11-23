#!/usr/bin/env python3
"""Deploy an exact main revision. Install as an OpenSSH forced command; see docs/deployment.md."""
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = None
LOGO_PATH = 'job-logo-worker.path'
LOGO_SERVICE = 'job-logo-worker.service'


def run(args, **kwargs):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                            timeout=900, **kwargs)
    if LOG:
        with LOG.open('a') as log:
            log.write(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed ({result.returncode}); inspect server deployment log')
    return result.stdout.strip()


def parse_revision(command):
    match = re.fullmatch(r'deploy ([0-9a-f]{40})', command)
    if not match:
        raise ValueError('Only deploy followed by a full lowercase commit SHA is allowed')
    return match[1]


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def switch(root, release):
    link = root / '.current-next'
    link.unlink(missing_ok=True)
    link.symlink_to(release)
    os.replace(link, root / 'current')


def backup_database(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    target_db = sqlite3.connect(destination)
    try:
        source_db.backup(target_db)
        if target_db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database backup integrity check failed')
    finally:
        source_db.close()
        target_db.close()


def schema(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    finally:
        db.close()


def service(action, unit):
    if action == 'is-active':
        result = subprocess.run(['systemctl', '--user', action, unit],
                                capture_output=True, text=True, timeout=30)
        state = result.stdout.strip()
        if state not in {'active', 'activating', 'deactivating', 'inactive', 'failed'}:
            raise RuntimeError('Cannot determine service state')
        return state
    if action in ('load-state', 'active-state'):
        property_name = 'LoadState' if action == 'load-state' else 'ActiveState'
        return run(['systemctl', '--user', 'show', f'--property={property_name}', '--value', unit])
    return run(['systemctl', '--user', action, unit])


def logo_path_active():
    if service('load-state', LOGO_PATH) == 'not-found':
        return False
    return service('is-active', LOGO_PATH) in {'active', 'activating'}


def pause_logo_worker():
    stopped = {'inactive', 'failed'}
    for unit in (LOGO_PATH, LOGO_SERVICE):
        if service('load-state', unit) == 'not-found' and service('active-state', unit) in stopped:
            continue
        service('stop', unit)
        if service('active-state', unit) not in stopped:
            raise RuntimeError(f'{unit} did not stop')


def hermes(action, config, reason):
    # Use the installed Hermes API, including its jobs-file locking and schedule semantics.
    code = '''
import json,sys
from cron.jobs import get_job,pause_job,resume_job
action,job_id,reason=sys.argv[1:]
job=get_job(job_id)
if job is None: raise RuntimeError('Configured Hermes job missing')
if action=='pause':
    if job.get('enabled'): pause_job(job_id, reason=reason)
elif action=='resume':
    if job.get('state')=='paused' and job.get('paused_reason')==reason: resume_job(job_id)
elif action!='get': raise ValueError('Invalid operation')
print(json.dumps({'enabled': bool(job.get('enabled'))}))
'''
    env = dict(os.environ, HERMES_HOME=config['hermes_home'])
    result = run([config['hermes_python'], '-c', code, action, config['job_id'], reason],
                 cwd=config['hermes_repo'], env=env)
    return json.loads(result.splitlines()[-1])


def busy(config):
    path = Path(config['hermes_home']) / 'cron/executions.db'
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    try:
        return bool(db.execute("SELECT 1 FROM executions WHERE job_id=? AND status IN ('claimed','running') LIMIT 1",
                               (config['job_id'],)).fetchone())
    finally:
        db.close()


def wait_idle(config, seconds=600):
    deadline = time.monotonic() + seconds
    while busy(config) or service('is-active', 'job-availability.service') not in {'inactive', 'failed'}:
        if time.monotonic() >= deadline:
            raise TimeoutError('Job still busy; retry deployment after it finishes')
        time.sleep(5)


def healthy(config, seconds=30):
    deadline = time.monotonic() + seconds
    request = Request('http://127.0.0.1:8765/healthz',
                      headers={'Host': urlsplit(config['base_url']).netloc})
    while True:
        try:
            with urlopen(request, timeout=3) as response:
                if response.status == 200 and json.load(response).get('status') == 'ok':
                    return True
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def restore_schedules(transaction, config, root):
    if transaction['resume_job']:
        hermes('resume', config, transaction['reason'])
    if transaction['restart_timer']:
        service('start', 'job-availability.timer')
    if (transaction['restart_logo_path']
            and (root / 'current/deploy' / LOGO_SERVICE).is_file()):
        for unit in (LOGO_SERVICE, LOGO_PATH):
            if service('load-state', unit) != 'loaded':
                raise RuntimeError(f'{unit} must be loaded to restore the logo watcher')
        service('start', LOGO_PATH)


def restore_web(transaction, root, previous, config):
    switch(root, previous)
    if transaction.get('web_was_active', True):
        service('restart', 'job-dashboard.service')
        return healthy(config)
    service('stop', 'job-dashboard.service')
    return True


def database_paths(root):
    return root / 'data/jobs.sqlite', root / 'data/dashboard/dashboard.sqlite'


def migrate_database(release, path, kind):
    code = 'import sys; from job_search.migrate_card_metadata import migrate_database; migrate_database(*sys.argv[1:])'
    run([release / '.venv-dashboard/bin/python', '-c', code, path, kind], cwd=release)


def migrate_fit_database(release, jobs):
    run([release / '.venv-dashboard/bin/python', '-m', 'job_search.collect_jobs',
         '--db', jobs, 'migrate-fit'], cwd=release)


def activate(root, release, config, *, migrate_cards=False, migrate_fit=False):
    journal = root / '.deploy/transaction.json'
    if journal.exists():
        raise RuntimeError('Interrupted deployment: run local --recover before deploying again')
    previous = (root / 'current').resolve(strict=True)
    if not (root / 'current').is_symlink():
        raise RuntimeError('Bootstrap a current release before enabling deployment')
    reason = 'job-search-deploy:' + release.name
    backup = root / '.deploy/backups' / release.name
    transaction = {'previous': str(previous), 'release': str(release), 'reason': reason,
                   'resume_job': bool(hermes('get', config, reason)['enabled']),
                   'restart_timer': service('is-active', 'job-availability.timer') in {'active', 'activating'},
                   'restart_logo_path': logo_path_active(),
                   'web_was_active': service('is-active', 'job-dashboard.service') in {'active', 'activating'},
                   'web_stopped': False, 'backup': str(backup), 'migrate_cards': migrate_cards,
                   'migrate_fit': migrate_fit}

    def checkpoint(phase):
        transaction['phase'] = phase
        write_json(journal, transaction)

    checkpoint('pausing_writers')
    clear_journal = False
    try:
        pause_logo_worker()
        if transaction['resume_job']:
            hermes('pause', config, reason)
        service('stop', 'job-availability.timer')
        wait_idle(config)
        if fetch_main(root, config)[1] != (release / '.revision').read_text().strip():
            raise RuntimeError('Main changed while waiting; deploy the newer workflow run')
        transaction['web_stopped'] = True
        checkpoint('backing_up')
        service('stop', 'job-dashboard.service')
        jobs, state = database_paths(root)
        backup_database(jobs, backup / 'jobs.sqlite')
        backup_database(state, backup / 'dashboard.sqlite')
        if migrate_cards:
            checkpoint('migrating_jobs')
            migrate_database(release, jobs, 'jobs')
            checkpoint('migrating_state')
            migrate_database(release, state, 'state')
        if migrate_fit:
            checkpoint('migrating_fit')
            migrate_fit_database(release, jobs)
        if migrate_cards or migrate_fit:
            checkpoint('checking_migration')
            validate_startup(root, release, config)
        checkpoint('starting_candidate')
        switch(root, release)
        service('start', 'job-dashboard.service')
        if not healthy(config):
            raise RuntimeError('New release failed health check')
        if not transaction['web_was_active']:
            service('stop', 'job-dashboard.service')
        checkpoint('restoring_schedules')
        clear_journal = True
    except BaseException:
        if transaction['web_stopped'] and not restore_web(transaction, root, previous, config):
            raise RuntimeError('Rollback health failed; inspect transaction.json and recover')
        clear_journal = True
        raise
    finally:
        # If restoring schedules fails or the process is killed, retain the journal for recovery.
        if clear_journal:
            restore_schedules(transaction, config, root)
            journal.unlink()


def recover(root, config):
    journal = root / '.deploy/transaction.json'
    transaction = json.loads(journal.read_text())
    previous = Path(transaction['previous']).resolve(strict=True)
    if previous.parent != (root / 'releases').resolve():
        raise RuntimeError('Recovery release must belong to this installation')
    if 'restart_logo_path' not in transaction:
        # Older deployers never changed the watcher's state.
        transaction['restart_logo_path'] = logo_path_active()
        write_json(journal, transaction)
    pause_logo_worker()
    if transaction['resume_job'] and hermes('get', config, transaction['reason'])['enabled']:
        hermes('pause', config, transaction['reason'])
    service('stop', 'job-availability.timer')
    wait_idle(config)
    if transaction['web_stopped']:
        if not restore_web(transaction, root, previous, config):
            raise RuntimeError('Recovery health check failed')
    restore_schedules(transaction, config, root)
    journal.unlink()


def environment(path):
    values = dict(os.environ)
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        parts = shlex.split(value, comments=True)
        if not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key) or len(parts) > 1:
            raise ValueError('Unsupported environment file syntax')
        values[key] = parts[0] if parts else ''
    return values


def check_release(root, release, config, jobs, state):
    before = [schema(jobs), schema(state)]
    env = environment(config['environment_file'])
    env.update(JOBS_DB=str(jobs), DASHBOARD_STATE_DB=str(state))
    env['COMPANY_RESEARCH'] = str(root / 'company_research.json')
    code = '''
from job_search.dashboard import create_production_app
app=create_production_app()
with app.test_client() as client:
    response=client.get('/healthz',base_url=app.config['BASE_URL'])
    assert response.status_code==200 and response.json['status']=='ok'
'''
    run([release / '.venv-dashboard/bin/python', '-c', code], cwd=release, env=env)
    if before != [schema(jobs), schema(state)]:
        raise RuntimeError('Schema change requires an explicit migration; automatic deployment refused')


def check_previous_writer(release, jobs, state):
    code = '''
import sqlite3,sys
from contextlib import closing
from datetime import datetime,timezone
from urllib.parse import parse_qs,urlsplit
from job_search.collect_jobs import collect
from job_search.refresh_job_availability import refresh

jobs,state=sys.argv[1:]
with closing(sqlite3.connect(jobs)) as db:
    item=db.execute("SELECT coalesce(max(id), 0)+1 FROM posts").fetchone()[0]
now=datetime.now(timezone.utc)
stamp=now.replace(tzinfo=None).isoformat()
post={"id":item,"link":f"https://inthiswork.com/archives/{item}","date_gmt":stamp,"modified_gmt":stamp,
      "title":{"rendered":"Migration Check Company｜Test role"},"content":{"rendered":"<p>지원 마감: 2099-12-01</p>"},
      "categories":[191700167]}
collect(jobs,lambda _:([post],1),now,advance_checkpoint=False)
with closing(sqlite3.connect(jobs)) as db,db:
    db.execute("UPDATE posts SET decision='recommend' WHERE id=?",(item,))
def fetch(url):
    ids=parse_qs(urlsplit(url).query).get("include",[""])[0].split(",")
    return ([post] if str(item) in ids else []),1
refresh(jobs,state,fetch,now)
with closing(sqlite3.connect(state)) as db:
    saved=db.execute("SELECT deadline_at FROM job_availability WHERE post_id=?",(item,)).fetchone()
    assert saved and saved[0].startswith("2099-12-01")
'''
    before = [schema(jobs), schema(state)]
    run([release / '.venv-dashboard/bin/python', '-c', code, jobs, state], cwd=release)
    if before != [schema(jobs), schema(state)]:
        raise RuntimeError('Previous release modified the migrated schema')


def validate_startup(root, release, config, *, migrate_cards=False, migrate_fit=False):
    check = root / '.deploy/checks' / release.name
    jobs, state = check / 'jobs.sqlite', check / 'dashboard.sqlite'
    source, personal = database_paths(root)
    backup_database(source, jobs)
    backup_database(personal, state)
    if migrate_cards:
        run([release / '.venv-dashboard/bin/python', '-m', 'job_search.migrate_card_metadata',
             '--jobs-db', jobs, '--state-db', state], cwd=release)
    if migrate_fit:
        migrate_fit_database(release, jobs)
    if migrate_cards or migrate_fit:
        previous = (root / 'current').resolve(strict=True)
        check_release(root, previous, config, jobs, state)
        check_previous_writer(previous, jobs, state)
    check_release(root, release, config, jobs, state)


def fetch_main(root, config):
    repository = root / '.deploy/repository.git'
    if not repository.exists():
        run(['git', 'init', '--bare', repository])
    run(['git', '--git-dir', repository, 'fetch', '--no-tags', '--depth=1',
         config['repository'], '+refs/heads/main:refs/heads/main'])
    return repository, run(['git', '--git-dir', repository, 'rev-parse', 'refs/heads/main'])


def deploy(root, revision, config, *, migrate_cards=False, migrate_fit=False):
    if (root / '.deploy/transaction.json').exists():
        raise RuntimeError('Interrupted deployment: run local --recover first')
    repository, latest = fetch_main(root, config)
    if revision != latest:
        raise RuntimeError('Requested revision is not current remote main')
    releases = root / 'releases'
    releases.mkdir(exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=revision + '-', dir=releases))
    print('Preparing release ' + revision, flush=True)
    archive = release / 'source.tar'
    run(['git', '--git-dir', repository, 'archive', '--format=tar', '-o', archive, revision])
    with tarfile.open(archive) as source:
        source.extractall(release, filter='data')
    archive.unlink()
    (release / '.revision').write_text(revision + '\n')
    run(['/usr/bin/python3', '-m', 'venv', release / '.venv-dashboard'])
    python = release / '.venv-dashboard/bin/python'
    run([python, '-m', 'pip', 'install', '-r', release / 'requirements.txt'])
    print('Testing candidate release', flush=True)
    run(['make', 'test', 'PYTHON=' + str(python)], cwd=release)
    validate_startup(root, release, config, migrate_cards=migrate_cards, migrate_fit=migrate_fit)
    for name in ['data', 'deployment.json', 'hermes_job_prompt.txt', 'company_research.json']:
        if (release / name).exists() or (release / name).is_symlink():
            raise RuntimeError('Release must not contain private runtime files')
        (release / name).symlink_to(root / name)
    # A newer main may have arrived while this candidate was preparing.
    if fetch_main(root, config)[1] != revision:
        raise RuntimeError('Main changed during preparation; deploy the newer workflow run')
    print('Waiting for jobs, backing up data, and activating', flush=True)
    activate(root, release, config, migrate_cards=migrate_cards, migrate_fit=migrate_fit)
    print('Deployed ' + revision + '; health verified', flush=True)


def main():
    global LOG
    os.umask(0o077)
    root = Path.home() / 'job_search'
    state = root / '.deploy'
    config = json.loads((state / 'config.json').read_text())
    command = os.environ.get('SSH_ORIGINAL_COMMAND')
    recovery = command is None and sys.argv[1:] == ['--recover']
    migrate_cards = command is None and sys.argv[1:2] == ['--migrate-card-metadata']
    migrate_fit = command is None and sys.argv[1:2] == ['--migrate-fit']
    arguments = sys.argv[2:] if migrate_cards or migrate_fit else sys.argv[1:]
    revision = None if recovery else parse_revision(command if command is not None else 'deploy ' + ' '.join(arguments))
    (state / 'logs').mkdir(exist_ok=True)
    LOG = state / 'logs' / (time.strftime('%Y%m%d-%H%M%S') + '.log')
    with (state / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def terminate(signum, frame):
            raise SystemExit('Deployment interrupted')
        signal.signal(signal.SIGTERM, terminate)
        if recovery:
            recover(root, config)
            print('Previous release and schedules recovered')
        else:
            deploy(root, revision, config, migrate_cards=migrate_cards, migrate_fit=migrate_fit)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Deployment stopped: {error}', file=sys.stderr)
        sys.exit(1)
