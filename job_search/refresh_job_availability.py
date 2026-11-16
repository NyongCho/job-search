"""Refresh availability for existing recommendations without rewriting reviews."""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .collect_jobs import API, FIELDS, extract, request_json
from .dashboard_store import init_state_db
from .job_availability import availability, inspect_document

ROOT = Path(__file__).resolve().parents[1]


def refresh(jobs_db, state_db, fetch_page=request_json, now=None):
    now = now or datetime.now(timezone.utc)
    init_state_db(jobs_db, state_db)
    with closing(sqlite3.connect(Path(state_db).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        saved = {r[0] for r in db.execute("SELECT post_id FROM job_state WHERE post_id>0 AND status IN ('interested','preparing','applied')")}
    with closing(sqlite3.connect(Path(jobs_db).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        candidates = [(r[0], json.loads(r[2])) for r in db.execute("SELECT id,decision,document FROM posts")
                      if r[1] == "recommend" or r[0] in saved]
    ids = sorted(i for i, doc in candidates if doc.get("source", "inthiswork") == "inthiswork"
                 and urlsplit(doc.get("url", "")).hostname == "inthiswork.com")
    unsupported = sorted(i for i, _ in candidates if i not in ids)
    checked = {}
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        url = API + "?" + urlencode({"include": ",".join(map(str, batch)), "per_page": 100, "_fields": FIELDS})
        posts, _ = fetch_page(url)
        if not isinstance(posts, list):
            raise ValueError("Expected Inthiswork posts")
        for post in posts:
            if (not isinstance(post, dict) or type(post.get("id")) is not int or post["id"] not in batch
                    or post["id"] in checked or not isinstance(post.get("categories"), list)
                    or any(type(c) is not int for c in post["categories"])):
                raise ValueError("Invalid Inthiswork availability response")
            for field in ("date_gmt", "modified_gmt"):
                value = post.get(field)
                if not isinstance(value, str) or "T" not in value:
                    raise ValueError("Invalid Inthiswork source timestamp")
                datetime.fromisoformat(value)
            doc = extract(post)
            checked[post["id"]] = (doc, inspect_document(doc))
    # Fetch every batch before starting the write transaction. A failed request preserves previous facts.
    with closing(sqlite3.connect(Path(state_db).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)) as db, db:
        for post_id, (doc, facts) in checked.items():
            db.execute("""INSERT INTO job_availability VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET closed=excluded.closed,deadline_at=excluded.deadline_at,
                evidence=excluded.evidence,checked_at=excluded.checked_at,source_modified_at=excluded.source_modified_at,
                source_url=excluded.source_url""", (post_id, int(facts["closed"]), facts["deadline_at"],
                facts["evidence"], now.isoformat(), doc.get("modified_gmt"), API + "/" + str(post_id)))
    return {"requested": len(ids), "checked": len(checked), "unsupported_ids": unsupported,
            "missing_ids": sorted(set(ids) - checked.keys()),
            "expired_in_checked": [i for i, (doc, _) in checked.items() if availability(doc, now=now)["is_expired"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-db", type=Path, default=ROOT / "data/jobs.sqlite")
    parser.add_argument("--state-db", type=Path, default=ROOT / "data/dashboard/dashboard.sqlite")
    args = parser.parse_args()
    print(json.dumps(refresh(args.jobs_db, args.state_db), ensure_ascii=False))


if __name__ == "__main__":
    main()
