"""Private job inbox. Production is exposed only through Tailscale Serve."""

import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException, SecurityError

from . import dashboard_store as store
from .company_logos import read_logo
from .company_research import load_research, lookup_research
from .refresh_job_availability import SourceCheckFailed, refresh_one

ROOT = Path(__file__).resolve().parents[1]
VIEW_LABELS = {"recommended": "추천", "interested": "관심", "preparing": "지원 준비",
               "applied": "지원 현황", "archived": "보관", "needs_review": "확인 필요", "expired": "만료"}
STATUS_LABELS = {"unmarked": "미지정", "interested": "관심", "preparing": "지원 준비",
                 "applied": "지원 완료", "archived": "보관"}
DECISION_LABELS = {"recommend": "추천", "needs_review": "확인 필요", "exclude": "추천 제외",
                   "below_threshold": "선호 밖 추천 기준 미만", None: "검토 대기"}
FIT_GROUP_LABELS = {"preferred": "희망 직무 추천", "outside": "선호 밖 추천", "legacy": "기존 추천 · 미산정",
                    "question": "확인 질문"}
FIT_CATEGORIES = {"work": "핵심 업무", "required": "필수 요건", "preferred": "우대 요건"}
FIT_STATUSES = {"met": "충족", "partial": "부분 충족", "unmet": "미충족", "unknown": "미확인", "tenure": "관련 경력"}


def _environment():
    dev = os.environ.get("DASHBOARD_DEV_MODE", "0").lower()
    if dev not in {"0", "1", "true", "false"}:
        raise ValueError("DASHBOARD_DEV_MODE must be 0 or 1")
    return {
        "JOBS_DB": Path(os.environ.get("JOBS_DB", ROOT / "data/jobs.sqlite")),
        "STATE_DB": Path(os.environ.get("DASHBOARD_STATE_DB", ROOT / "data/dashboard/dashboard.sqlite")),
        "BASE_URL": os.environ.get("DASHBOARD_BASE_URL", ""),
        "ALLOWED_LOGIN": os.environ.get("DASHBOARD_ALLOWED_LOGIN", ""),
        "SECRET_KEY": os.environ.get("DASHBOARD_SECRET_KEY", ""),
        "DEV_MODE": dev in {"1", "true"},
        "COMPANY_RESEARCH": Path(os.environ.get("COMPANY_RESEARCH", ROOT / "company_research.json")),
    }


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(_environment())
    app.config.update(config or {})
    research = load_research(app.config["COMPANY_RESEARCH"])
    base = app.config["BASE_URL"]
    if not store.safe_url(base):
        raise ValueError("A valid dashboard base URL is required")
    parsed = urlsplit(base)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("The dashboard requires a root URL without query/fragment")
    dev = app.config["DEV_MODE"]
    if dev:
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Development mode requires an HTTP loopback URL")
    elif parsed.scheme != "https" or not app.config["ALLOWED_LOGIN"].strip():
        raise ValueError("Production requires HTTPS and an allowed Tailscale login")
    if not isinstance(app.config["SECRET_KEY"], str) or len(app.config["SECRET_KEY"]) < 32:
        raise ValueError("A persistent random session key of at least 32 characters is required")
    app.config.update(
        DEBUG=False, TRUSTED_HOSTS=[parsed.hostname], MAX_CONTENT_LENGTH=16384,
        SESSION_COOKIE_NAME="job_dashboard_session", SESSION_COOKIE_SECURE=not dev,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    )
    source, state = Path(app.config["JOBS_DB"]), Path(app.config["STATE_DB"])
    store.init_state_db(source, state)

    @app.before_request
    def authorize():
        if request.endpoint == "health":
            return
        if dev:
            if request.remote_addr not in {"127.0.0.1", "::1"}:
                abort(403)
        elif request.headers.get("Tailscale-User-Login", "") != app.config["ALLOWED_LOGIN"]:
            abort(403)
        g.profile_name = "로컬 사용자" if dev else app.config["ALLOWED_LOGIN"].partition("@")[0]
        if request.method == "POST":
            token, expected = request.headers.get("X-CSRF-Token", ""), session.get("csrf_token", "")
            if not token or not expected or not hmac.compare_digest(token, expected):
                abort(403)

    @app.after_request
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        })
        return response

    @app.template_filter("kst")
    def kst(value, compact=False):
        canonical = store.timestamp(value)
        if not canonical:
            return "미확인"
        date = datetime.fromisoformat(canonical).astimezone(timezone(timedelta(hours=9)))
        return date.strftime("%m.%d" if compact else "%Y.%m.%d %H:%M")

    def context():
        view, q = request.args.get("view", "recommended"), request.args.get("q", "")
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            raise ValueError("페이지 번호를 확인해 주세요.") from None
        if view not in store.VIEWS or len(q) > 200 or page < 1:
            raise ValueError("보기, 검색어 또는 페이지를 확인해 주세요.")
        return view, q, page, store.resolve_sort(view, request.args.get("sort"))

    def screen(post_id=None):
        view, q, page, sort = context()
        listing = store.list_jobs(source, state, view=view, q=q, page=page, sort=sort)
        detail = store.get_job(source, state, post_id) if post_id is not None else None
        if post_id is not None and detail is None:
            abort(404)
        for item in listing["items"]:
            item["company_review"] = lookup_research(research, item["company"])
        if detail is not None:
            detail["company_review"] = lookup_research(research, detail["company"])
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return render_template("dashboard.html", listing=listing, detail=detail, view=view, q=q,
                               sort_options=store.sort_options(view),
                               views=VIEW_LABELS, statuses=STATUS_LABELS, decisions=DECISION_LABELS,
                               fit_groups=FIT_GROUP_LABELS, fit_categories=FIT_CATEGORIES, fit_statuses=FIT_STATUSES,
                               stages=store.STAGE_LABELS, results=store.RESULT_LABELS,
                               error=None)

    @app.get("/")
    def index():
        return screen()

    @app.get("/jobs/<int(signed=True):post_id>")
    def detail(post_id):
        return screen(post_id)

    @app.get("/jobs/<int:post_id>/logo")
    def company_logo(post_id):
        image = read_logo(source, post_id)
        if image is None:
            abort(404)
        return Response(image, mimetype="image/png")

    @app.post("/jobs/<int(signed=True):post_id>/state")
    def change_state(post_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            raise ValueError("지원 상태를 확인해 주세요.")
        return jsonify(store.set_status(source, state, post_id, data["status"]))

    @app.post("/jobs/<int(signed=True):post_id>/seen")
    def seen(post_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("reviewed_at"), str):
            raise ValueError("검토 시각을 확인해 주세요.")
        return jsonify(recorded=store.mark_seen(source, state, post_id, data["reviewed_at"]))

    @app.post("/jobs/<int(signed=True):post_id>/progress")
    def change_progress(post_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("전형 정보를 확인해 주세요.")
        return jsonify(store.set_stage(source, state, post_id, data.get("stage"), data.get("result"),
                                       data.get("event_date", ""), data.get("note", "")))

    @app.post("/jobs/<int(signed=True):post_id>/deadline")
    def change_deadline(post_id):
        context()
        try:
            result = store.set_deadline(source, state, post_id, request.get_json(silent=True))
        except store.DeadlineChanged as error:
            return jsonify(error=409, message=str(error), html=screen(post_id)), 409
        return jsonify(**result, html=screen(post_id), message="마감 정보를 저장했어요.")

    @app.post("/jobs/<int(signed=True):post_id>/deadline/refresh")
    def refresh_deadline(post_id):
        context()
        try:
            updated = refresh_one(source, state, post_id)
        except SourceCheckFailed as error:
            return error_response(502, str(error))
        message = "마감 정보를 다시 확인했어요." if updated else "더 최근에 확인한 마감 정보를 표시했어요."
        return jsonify(html=screen(post_id), message=message)

    @app.get("/healthz")
    def health():
        try:
            store.check_card_metadata(source, state)
            store.list_jobs(source, state)
        except (OSError, sqlite3.Error, ValueError):
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok")

    def error_response(code, message):
        if request.method == "POST":
            return jsonify(error=code, message=message), code
        return render_template("dashboard.html", error={"code": code, "message": message},
                               listing=None, detail=None, view="recommended", q="", views=VIEW_LABELS,
                               statuses=STATUS_LABELS, decisions=DECISION_LABELS), code

    @app.errorhandler(HTTPException)
    def http_error(error):
        if isinstance(error, SecurityError):
            # Host rejection happens before Flask can build template URLs.
            return "허용되지 않은 호스트입니다.", 400
        messages = {400: "요청 주소를 확인해 주세요.", 403: "접근 권한을 확인해 주세요. Tailscale의 허용된 계정으로 접속할 수 있어요.",
                    404: "이 공고를 찾을 수 없어요.", 405: "지원하지 않는 요청이에요.", 413: "요청이 너무 커요."}
        return error_response(error.code, messages.get(error.code, "요청을 처리하지 못했어요."))

    @app.errorhandler(ValueError)
    def invalid(error):
        return error_response(400, str(error))

    @app.errorhandler(LookupError)
    def missing(error):
        return error_response(404, "이 공고를 찾을 수 없어요.")

    @app.errorhandler(OSError)
    @app.errorhandler(sqlite3.Error)
    def unavailable(error):
        return error_response(503, "지금은 데이터를 불러오거나 저장할 수 없어요. 잠시 후 다시 시도해 주세요.")

    return app


def create_production_app():
    config = _environment()
    if config["DEV_MODE"]:
        raise ValueError("Development bypass is disabled in the production entry point")
    app = create_app(config)
    try:
        store.check_card_metadata(config["JOBS_DB"], config["STATE_DB"])
    except sqlite3.OperationalError as error:
        if str(error).startswith(("no such table:", "no such column:")):
            raise RuntimeError("Run job_search.migrate_card_metadata before starting this release") from error
        raise
    return app


if __name__ == "__main__":
    app = create_app()
    if not app.config["DEV_MODE"]:
        raise SystemExit("Use Waitress with create_production_app for production")
    port = urlsplit(app.config["BASE_URL"]).port or 8765
    app.run(host="127.0.0.1", port=port, debug=False)
