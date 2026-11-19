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


def create_logo_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS company_logos (
        company_key TEXT PRIMARY KEY, source_page TEXT NOT NULL, image_url TEXT NOT NULL,
        image BLOB NOT NULL, checked_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS job_company_logos (
        post_id INTEGER PRIMARY KEY CHECK(post_id > 0), fingerprint TEXT NOT NULL,
        document_version TEXT NOT NULL, identity TEXT NOT NULL,
        company_key TEXT REFERENCES company_logos(company_key),
        outcome TEXT NOT NULL CHECK(outcome IN ('pending','ready','missing')),
        attempted_at REAL, lease_until REAL NOT NULL DEFAULT 0
    )""")


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
