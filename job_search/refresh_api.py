"""Authenticated dashboard endpoints for starting and following persistent refreshes."""
from contextlib import closing
from uuid import UUID

from flask import jsonify, render_template, request

from . import dashboard_store as store, refresh_queue as queue
from .logo_scope import eligible_post_ids


def _scope(view, q, post_id):
    return f"detail:{post_id}" if post_id is not None else f"list:{view}:{q.strip().casefold()}"


def find_runs(source, state, view, q, post_id=None):
    if post_id is not None:
        return queue.find(state, _scope(view, q, post_id), [post_id], post_id=post_id)
    with closing(queue.connect(state)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='refresh_runs'").fetchone():
            return []
        if not db.execute("SELECT 1 FROM refresh_runs LIMIT 1").fetchone():
            return []
    ids = [post for post, _ in store.refresh_targets(source, state, view=view, q=q)]
    return queue.find(state, _scope(view, q, None), ids)


def register(app, source, state, context):
    def selected_post():
        raw = request.args.get("post_id")
        post_id = int(raw) if raw is not None else None
        if post_id is not None and not -(2**63) <= post_id < 2**63:
            raise ValueError("공고 번호를 확인해 주세요.")
        return post_id

    def enqueue(post_id):
        if post_id is not None and not -(2**63) <= post_id < 2**63:
            raise ValueError("공고 번호를 확인해 주세요.")
        view, q, _, _ = context()
        targets = store.refresh_targets(source, state, view=view, q=q, post_id=post_id)
        request_id = request.headers.get("X-Refresh-Request")
        run = queue.enqueue(state, targets, _scope(view, q, post_id), str(UUID(request_id)) if request_id else None,
                            logo_post_ids=eligible_post_ids(source, state, post_id))
        return jsonify(queue.snapshot(state, run, post_id=post_id)), 202

    @app.post("/refreshes")
    def start_refresh():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {"post_id"}:
            raise ValueError("새로고침 대상을 확인해 주세요.")
        post_id = data.get("post_id")
        if "post_id" in data and type(post_id) is not int:
            raise ValueError("공고 번호를 확인해 주세요.")
        return enqueue(post_id)

    @app.post("/jobs/<int(signed=True):post_id>/deadline/refresh")
    def refresh_deadline(post_id):
        return enqueue(post_id)

    @app.get("/refreshes")
    def refreshes():
        if "request" in request.args:
            return jsonify(runs=queue.receipt(state, str(UUID(request.args["request"]))))
        view, q, _, _ = context()
        return jsonify(runs=find_runs(source, state, view, q, selected_post()))

    @app.get("/refreshes/<int:run>")
    def refresh_status(run):
        after = int(request.args.get("after", 0))
        if not 0 <= after < 2**63 or run >= 2**63:
            raise ValueError("진행 상태 번호를 확인해 주세요.")
        view = request.args.get("view")
        report = view == "usage"
        status = queue.snapshot(state, run, None if report else after, post_id=selected_post())
        if view == "progress":
            return jsonify(status)
        from .fit_work import COUNTERS, usage
        attempts = usage(source, queue.fit_work_ids(state, run), state)
        with closing(queue.connect(state)) as db:
            for attempt in attempts:
                owner = attempt.pop("owner_work_id", None)
                owner_run = attempt.pop("owner_run_id", None)
                if owner_run is None and owner is not None:
                    owner_run = db.execute("""SELECT min(run_id) FROM refresh_members m
                        JOIN refresh_work w USING(group_id) WHERE w.id=?""", (owner,)).fetchone()[0]
                attempt["owned"] = owner_run == run
        if report:
            owned = [attempt for attempt in attempts if attempt["owned"]]
            totals = {"submitted": sum(attempt["submitted"] for attempt in owned)}
            for name in COUNTERS:
                values = [attempt[name] for attempt in owned]
                totals[name] = None if None in values else sum(values)
            return render_template("refresh_usage.html", run=run, complete=status["complete"],
                                   totals=totals, shared=len(attempts) - len(owned))
        status["usage"] = attempts
        return jsonify(status)

    @app.get("/refreshes/activity")
    def activity():
        return jsonify(queue.activity(state))
