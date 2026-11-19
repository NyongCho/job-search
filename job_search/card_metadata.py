"""Shared table definitions for new databases and the explicit migration."""


def create_deadline_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS job_deadline_facts (
        post_id INTEGER PRIMARY KEY CHECK(post_id > 0),
        checked_at TEXT NOT NULL, document_version TEXT NOT NULL,
        deadline_kind TEXT NOT NULL CHECK(deadline_kind IN ('datetime','date','rolling','unknown')),
        deadline_word TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS job_deadline_overrides (
        post_id INTEGER PRIMARY KEY, deadline_at TEXT,
        deadline_kind TEXT NOT NULL CHECK(deadline_kind IN ('datetime','date','rolling','unknown')),
        deadline_word TEXT NOT NULL
    )""")


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
