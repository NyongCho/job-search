"""Select collected jobs whose dashboard category warrants looking up a company logo."""

import json
import os
from contextlib import closing
from pathlib import Path


TRACKED_STATUSES = ("interested", "preparing", "applied")


def default_state_path(jobs_path):
    return Path(os.environ.get("DASHBOARD_STATE_DB") or Path(jobs_path).parent / "dashboard/dashboard.sqlite")


def eligible_post_ids(jobs_path, state_path, post_id=None):
    # Dashboard rendering also imports the logo cache. Load its view rules at query time.
    from . import dashboard_store as store

    if post_id is not None and post_id <= 0:
        return set()
    source, state = store._validate_paths(jobs_path, state_path)
    one = post_id is not None
    args = (post_id,) if one else ()
    with closing(store._connect(state, "ro")) as db:
        statuses = dict(db.execute("SELECT post_id,status FROM job_state WHERE post_id>0" +
                                   (" AND post_id=?" if one else ""), args))
        cached = store._availability_rows(db, post_id)
        overrides = store._deadline_overrides(db, post_id)

    tracked = [i for i, status in statuses.items() if status in TRACKED_STATUSES]
    with closing(store._connect(source, "ro")) as db:
        eligible = set()
        if tracked:
            eligible.update(row[0] for row in db.execute(
                "SELECT id FROM posts WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(tracked),)))
        if one and statuses.get(post_id, "unmarked") != "unmarked":
            return eligible
        # A closed scored recommendation can reappear after a deadline correction.
        scored = any(row["name"] == "fit_review" for row in db.execute("PRAGMA table_info(posts)"))
        reopened = """ OR CASE WHEN json_valid(fit_review)
                           THEN json_extract(fit_review,'$.availability') END='closed'""" if scored else ""
        candidates = "SELECT * FROM posts WHERE id>0 AND (decision='recommend'" + reopened + ")"
        for row in db.execute(candidates + (" AND id=?" if one else ""), args):
            i = row["id"]
            if statuses.get(i, "unmarked") == "unmarked":
                item = store._job(row, {}, cached.get(i), overrides.get(i), refresh_scope=("recommended", ""))
                if store._matches(item, "recommended"):
                    eligible.add(i)
    return eligible
