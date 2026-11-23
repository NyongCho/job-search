"""Run durable dashboard refresh tasks independently of the HTTP request."""
import argparse
import fcntl
import logging
import os
import signal
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from functools import partial
from pathlib import Path

from . import fit_work
from . import refresh_queue as queue

ROOT = Path(__file__).resolve().parents[1]


def serve(jobs, state, stop, *, deadlines, logos, fits, poll=0.5, fit_batch=fit_work.DEFAULT_BATCH_SIZE):
    def run(kind, batch):
        finish = partial(queue.finish, state)
        while not stop.is_set():
            tasks = queue.claim(state, kind, batch)
            if not tasks:
                stop.wait(poll)
                continue
            try:
                if kind == "deadline":
                    deadlines(jobs, state, tasks, finish)
                elif kind == "fit":
                    fits(jobs, state, tasks, finish)
                elif logos(jobs, state, tasks[0], finish) is False:
                    queue.defer(state, tasks[0])
            except Exception as error:
                if (isinstance(error, sqlite3.OperationalError)
                        and getattr(error, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)):
                    # The domain result may already be committed. Replay its receipt after contention clears.
                    for task in tasks:
                        queue.defer(state, task)
                    continue
                logging.exception("Refresh %s failed for work IDs %s", kind, [t["id"] for t in tasks])
                for task in tasks:
                    finish(task, "failed", "작업을 완료하지 못했어요. 워커 실행 기록을 확인해 주세요.")

    lock_path = Path(state).resolve().with_suffix(".refresh-worker.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue.reset_running(state)
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(run, kind, batch) for kind, batch in
                       (("deadline", 25), ("deadline", 25), ("logo", 1), ("logo", 1), ("fit", fit_batch))]
            try:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
            finally:
                stop.set()


def migrate(jobs, state, *, schema_only=False):
    from .migrate_card_metadata import migrate_database
    from .dashboard_store import init_state_db
    jobs, state = Path(jobs).resolve(strict=True), Path(state).resolve()
    if state.exists() and jobs.samefile(state):
        raise ValueError("The jobs and dashboard databases must be different files")
    init_state_db(jobs, state)
    migrate_database(jobs, "jobs")
    migrate_database(state, "state")
    with closing(queue.connect(state)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        queue.create_tables(db)
    fit_work.migrate(jobs, state, profile={} if schema_only else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-db", type=Path, default=os.environ.get("JOBS_DB", ROOT / "data/jobs.sqlite"))
    parser.add_argument("--state-db", type=Path,
                        default=os.environ.get("DASHBOARD_STATE_DB", ROOT / "data/dashboard/dashboard.sqlite"))
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--schema-only", action="store_true", help="Use an empty profile on a disposable validation copy")
    args = parser.parse_args()
    if args.migrate:
        migrate(args.jobs_db, args.state_db, schema_only=args.schema_only)
        return
    if args.schema_only:
        parser.error("--schema-only requires --migrate")
    from . import refresh_sources
    batch = int(os.environ.get("FIT_BATCH_SIZE", fit_work.DEFAULT_BATCH_SIZE))
    slots = int(os.environ.get("FIT_CONCURRENCY", "1"))
    if not 1 <= slots <= 4:
        parser.error("FIT_CONCURRENCY must be 1 through 4")
    if not 1 <= batch <= 5:
        parser.error("FIT_BATCH_SIZE must be 1 through 5")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    serve(args.jobs_db, args.state_db, stop, deadlines=refresh_sources.refresh_deadlines,
          logos=refresh_sources.refresh_logo_task,
          fits=partial(fit_work.refresh_fit_tasks, batch_size=batch, concurrency=slots), fit_batch=batch * slots)


if __name__ == "__main__":
    main()
