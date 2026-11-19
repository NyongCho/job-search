"""Inthiswork JD collection and evidence-backed review queue (stdlib only)."""

import argparse
import hashlib
import html
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .evidence import Document, application_link
from .job_sources import INTHISWORK_API

API = INTHISWORK_API
FIELDS = "id,date_gmt,modified_gmt,link,title,content,categories"
JOB_CATEGORIES = {191700167, 191700168}
CLOSED = 191700169
KST = timezone(timedelta(hours=9))
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data/jobs.sqlite"


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY, document TEXT NOT NULL, fingerprint TEXT NOT NULL,
            decision TEXT, reason TEXT, evidence_lines TEXT, reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, attempts TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0
        );
    """)
    return db


def request_json(url):
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "PersonalJobReader/0.1"})
            with urlopen(request, timeout=45) as response:
                return json.load(response), int(response.headers.get("X-WP-TotalPages", "1"))
        except HTTPError as error:
            if attempt == 2 or error.code not in {429, 500, 502, 503, 504}:
                raise
            retry = error.headers.get("Retry-After", "")
            delay = int(retry) if retry.isdigit() else 5 * (2 ** attempt)
            # Leave long server-requested waits to the next scheduled run.
            if delay > 60:
                raise
            time.sleep(max(delay, 1))


def checkpoint(path):
    with closing(connect(path)) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='last_success'").fetchone()
        return row[0] if row else None


def extract(post):
    document = Document(post["content"]["rendered"], post["link"])
    lines = document.lines
    return {
        "id": int(post["id"]), "title": html.unescape(post["title"]["rendered"]),
        "url": post["link"], "published_gmt": post["date_gmt"],
        "modified_gmt": post["modified_gmt"], "categories": post["categories"],
        "lines": lines, "links": document.links, "images": document.images,
        "apply_url": application_link(document, "지원하러가기"),
        "content_status": "text_available" if len("".join(lines)) >= 80 else "needs_review",
    }


def collect(path, fetch_page=request_json, now=None, *, start=None, advance_checkpoint=True):
    now = now or datetime.now(timezone.utc)
    previous = checkpoint(path)
    if start is None:
        start = datetime.fromisoformat(previous) - timedelta(hours=24) if previous else now - timedelta(hours=48)
    params = {"per_page": 100, "orderby": "date", "order": "asc", "_fields": FIELDS,
              "after": start.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S"),
              "before": now.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S")}
    documents = {}
    page, total_pages = 1, 1
    while page <= total_pages:
        posts, total_pages = fetch_page(API + "?" + urlencode({**params, "page": page}))
        if not isinstance(posts, list):
            raise ValueError("Expected a list of Inthiswork posts")
        for post in posts:
            categories = set(post["categories"])
            if categories & JOB_CATEGORIES and CLOSED not in categories:
                documents[int(post["id"])] = extract(post)
        page += 1

    result = {"new": 0, "changed": 0, "fetched_jobs": len(documents)}
    with closing(connect(path)) as db, db:
        for post_id, doc in documents.items():
            serialized = json.dumps(doc, ensure_ascii=False, sort_keys=True)
            fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
            old = db.execute("SELECT fingerprint FROM posts WHERE id=?", (post_id,)).fetchone()
            if old is None:
                db.execute("INSERT INTO posts(id,document,fingerprint) VALUES(?,?,?)",
                           (post_id, serialized, fingerprint))
                result["new"] += 1
            elif old[0] != fingerprint:
                db.execute("""UPDATE posts SET document=?,fingerprint=?,decision=NULL,reason=NULL,
                           evidence_lines=NULL,reviewed_at=NULL WHERE id=?""", (serialized, fingerprint, post_id))
                result["changed"] += 1
        if advance_checkpoint:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('last_success',?)", (now.isoformat(),))
    if not advance_checkpoint:
        result["ids"] = sorted(documents)
    return result


def prepare_backfill(path, manifest, days, fetch_page=request_json, now=None):
    manifest = Path(manifest)
    if manifest.exists():
        raise FileExistsError(f"Preserving existing backfill manifest: {manifest}")
    if type(days) is not int or days < 1:
        raise ValueError("Backfill days must be a positive integer")
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    result = collect(path, fetch_page, now, start=start, advance_checkpoint=False)
    payload = {"start_kst": start.astimezone(KST).isoformat(),
               "end_kst": now.astimezone(KST).isoformat(), "ids": result.pop("ids")}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("x") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return {**result, "manifest": str(manifest), "total": len(payload["ids"])}


def pending(path, limit=50):
    with closing(connect(path)) as db:
        rows = db.execute("SELECT document FROM posts WHERE decision IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [{key: doc[key] for key in ("id", "title", "url", "content_status")}
                for row in rows for doc in [json.loads(row[0])]]


def get_posts(path, ids):
    with closing(connect(path)) as db:
        result = []
        for post_id in ids:
            row = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown post: {post_id}")
            result.append({**json.loads(row["document"]), "fingerprint": row["fingerprint"],
                           "decision": row["decision"], "reason": row["reason"]})
        return result


def record_reviews(path, entries, digest=None, execution_id=None):
    if not isinstance(entries, list) or not entries:
        raise ValueError("Review file must be a nonempty JSON array")
    if digest is not None and (not digest.strip() or digest.strip() == "[SILENT]" or not execution_id):
        raise ValueError("A nonempty digest and execution ID are required")
    with closing(connect(path)) as db, db:
        seen = set()
        for entry in entries:
            post_id, decision = entry["id"], entry["decision"]
            row = db.execute("SELECT document,fingerprint FROM posts WHERE id=?", (post_id,)).fetchone()
            if not row or post_id in seen:
                raise ValueError(f"Unknown or duplicate post: {post_id}")
            seen.add(post_id)
            doc = json.loads(row[0])
            reason, lines = entry.get("reason", ""), entry.get("evidence_lines", [])
            if decision not in {"recommend", "exclude", "needs_review"} or not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"Invalid decision/reason for {post_id}")
            if not isinstance(lines, list) or any(type(n) is not int or not 1 <= n <= len(doc["lines"]) for n in lines):
                raise ValueError(f"Invalid evidence lines for {post_id}")
            if decision != "needs_review" and not lines:
                raise ValueError(f"Decision requires JD evidence: {post_id}")
            if not isinstance(entry.get("fingerprint"), str) or entry["fingerprint"] != row[1]:
                raise ValueError(f"JD changed since review: {post_id}")
            db.execute("UPDATE posts SET decision=?,reason=?,evidence_lines=?,reviewed_at=? WHERE id=?",
                       (decision, reason, json.dumps(lines), datetime.now(timezone.utc).isoformat(), post_id))
        if digest is not None:
            db.execute("INSERT INTO outbox(id,digest,attempts) VALUES(?,?,?)",
                       (execution_id, digest, json.dumps([execution_id])))


def reconcile_outbox(path, outcomes, execution_id):
    """Keep unacknowledged digests across failed runs; Hermes remains the sender."""
    retry = []
    with closing(connect(path)) as db, db:
        for row in db.execute("SELECT * FROM outbox WHERE delivered=0 ORDER BY rowid").fetchall():
            attempts = json.loads(row["attempts"])
            if any(outcomes.get(attempt) == "delivered" for attempt in attempts):
                db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (row["id"],))
            else:
                retry.append(row["digest"])
                if execution_id not in attempts:
                    db.execute("UPDATE outbox SET attempts=? WHERE id=?",
                               (json.dumps([*attempts, execution_id]), row["id"]))
    return retry


def status(path):
    with closing(connect(path)) as db:
        return {row[0] or "pending": row[1] for row in db.execute("SELECT decision,count(*) FROM posts GROUP BY decision")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect")
    backfill = sub.add_parser("prepare-backfill")
    backfill.add_argument("--days", type=int, default=14)
    backfill.add_argument("--manifest", type=Path, required=True)
    queue = sub.add_parser("pending")
    queue.add_argument("--limit", type=int, default=50)
    sub.add_parser("status")
    show = sub.add_parser("show")
    show.add_argument("ids", type=int, nargs="+")
    review = sub.add_parser("review")
    review.add_argument("file", type=Path)
    review.add_argument("--digest", type=Path)
    review.add_argument("--execution-id")
    args = parser.parse_args()
    if args.command == "collect":
        result = {"collection": collect(args.db), "status": status(args.db), "pending": pending(args.db)}
    elif args.command == "prepare-backfill":
        result = prepare_backfill(args.db, args.manifest, args.days)
    elif args.command == "pending":
        result = pending(args.db, args.limit)
    elif args.command == "show":
        result = get_posts(args.db, args.ids)
        for doc in result:
            doc["lines"] = [{"line": n, "text": text} for n, text in enumerate(doc["lines"], 1)]
    elif args.command == "review":
        entries = json.loads(args.file.read_text())
        if any(entry.get("decision") == "recommend" for entry in entries) and not args.digest:
            raise ValueError("Recommendations require --digest FILE --execution-id ID for delivery recovery")
        record_reviews(args.db, entries, args.digest.read_text() if args.digest else None, args.execution_id)
        result = status(args.db)
    else:
        result = status(args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
