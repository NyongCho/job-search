"""Add deadline and company logo tables to an existing pair of SQLite files."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from .card_metadata import create_deadline_tables, create_logo_tables

_DATABASES = {
    "jobs": ("SELECT id,document,fingerprint FROM posts LIMIT 0", create_logo_tables),
    "state": ("SELECT post_id,checked_at FROM job_availability LIMIT 0", create_deadline_tables),
}


def _open(path, mode):
    return sqlite3.connect(Path(path).resolve(strict=True).as_uri() + f"?mode={mode}", uri=True, timeout=2)


def migrate_database(path, kind):
    validate, create = _DATABASES[kind]
    with closing(_open(path, "rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(validate)
        create(db)


def migrate(jobs_db, state_db):
    jobs, state = Path(jobs_db).resolve(strict=True), Path(state_db).resolve(strict=True)
    if jobs.samefile(state):
        raise ValueError("The jobs and personal state databases must be different files")
    for kind, path in (("jobs", jobs), ("state", state)):
        with closing(_open(path, "ro")) as db:
            db.execute(_DATABASES[kind][0])
    migrate_database(jobs, "jobs")
    migrate_database(state, "state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-db", required=True, type=Path)
    parser.add_argument("--state-db", required=True, type=Path)
    args = parser.parse_args()
    migrate(args.jobs_db, args.state_db)
    print("Job card metadata tables ready.")


if __name__ == "__main__":
    main()
