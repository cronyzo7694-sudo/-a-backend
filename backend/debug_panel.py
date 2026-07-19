"""Temporary Flask diagnostics panel for Exam OS.

Integration in app.py:
    from debug_panel import debug_bp, install_debug_handlers
    app.register_blueprint(debug_bp)
    install_debug_handlers(app)

Open:
    /debug
    /debug/routes
    /debug/health

Remove this file and its registration before production, or protect it with
DEBUG_PANEL_TOKEN environment variable.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import platform
import sys
import traceback
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request
from werkzeug.exceptions import HTTPException


debug_bp = Blueprint("debug_panel", __name__)


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _authorized() -> bool:
    """Allow localhost, or require DEBUG_PANEL_TOKEN when configured."""
    token = os.getenv("DEBUG_PANEL_TOKEN", "").strip()
    if not token:
        # Convenient during initial Render debugging. Set a token before sharing.
        return True
    supplied = request.args.get("token", "") or request.headers.get("X-Debug-Token", "")
    return supplied == token


def _denied():
    return jsonify({"ok": False, "error": "Debug panel unauthorized"}), 401


def _safe_env() -> dict[str, str]:
    """Show useful environment values without exposing secrets."""
    allowed = {
        "PORT", "HOST", "FLASK_ENV", "FLASK_DEBUG", "RENDER",
        "RENDER_SERVICE_NAME", "RENDER_GIT_COMMIT", "PYTHON_VERSION",
        "DATABASE_URL", "FRONTEND_URL", "CORS_ORIGINS",
    }
    secret_words = ("KEY", "SECRET", "PASSWORD", "TOKEN", "JWT", "CREDENTIAL")
    result: dict[str, str] = {}
    for key in sorted(os.environ):
        if key not in allowed:
            continue
        if any(word in key.upper() for word in secret_words):
            result[key] = "[hidden]"
        elif key == "DATABASE_URL":
            value = os.getenv(key, "")
            # Never print database credentials.
            result[key] = value.split("@")[-1] if "@" in value else "[configured]"
        else:
            result[key] = os.getenv(key, "")
    return result


def _route_data() -> list[dict[str, Any]]:
    rows = []
    for rule in sorted(current_app.url_map.iter_rules(), key=lambda x: str(x)):
        rows.append({
            "rule": str(rule),
            "endpoint": rule.endpoint,
            "methods": sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}),
        })
    return rows


@debug_bp.route("/debug", methods=["GET"])
def debug_page():
    if not _authorized():
        return _denied()

    routes = _route_data()
    env = _safe_env()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    checks = {
        "GET /": "not tested",
        "GET /health": "not tested",
        "database": "not tested",
    }

    # Try common SQLAlchemy setup, but do not make it a requirement.
    try:
        db = current_app.extensions.get("sqlalchemy")
        if db is not None:
            with db.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            checks["database"] = "OK"
        else:
            checks["database"] = "SQLAlchemy extension not found"
    except Exception as exc:
        checks["database"] = f"ERROR: {type(exc).__name__}: {exc}"

    route_rows = "".join(
        f"<tr><td><code>{_e(r['rule'])}</code></td>"
        f"<td>{_e(r['endpoint'])}</td><td>{_e(', '.join(r['methods']))}</td></tr>"
        for r in routes
    )
    env_rows = "".join(
        f"<tr><td><code>{_e(k)}</code></td><td>{_e(v)}</td></tr>"
        for k, v in env.items()
    ) or "<tr><td colspan='2'>No selected environment variables found</td></tr>"
    check_rows = "".join(
        f"<tr><td><code>{_e(k)}</code></td><td>{_e(v)}</td></tr>"
        for k, v in checks.items()
    )

    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Exam OS Debug Panel</title>
<style>
body{{font-family:Arial,sans-serif;background:#101827;color:#e5e7eb;margin:0;padding:24px}}
main{{max-width:1100px;margin:auto}} h1{{color:#67e8f9}} h2{{color:#a5b4fc;margin-top:28px}}
.card{{background:#1f2937;border:1px solid #374151;border-radius:10px;padding:16px;margin:14px 0;overflow:auto}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;border-bottom:1px solid #374151;text-align:left;vertical-align:top}}
th{{color:#93c5fd}} code{{color:#fcd34d}} .muted{{color:#9ca3af}}
</style></head><body><main>
<h1>Exam OS Backend Debug Panel</h1>
<p class="muted">Generated: {_e(now)} | Python: {_e(platform.python_version())} | Flask app: {_e(current_app.name)}</p>
<div class="card"><h2>Quick endpoints</h2><p><a href="/debug/health">/debug/health</a> &nbsp; <a href="/debug/routes">/debug/routes</a></p></div>
<div class="card"><h2>Checks</h2><table><tr><th>Check</th><th>Result</th></tr>{check_rows}</table></div>
<div class="card"><h2>Registered routes ({len(routes)})</h2><table><tr><th>Path</th><th>Endpoint</th><th>Methods</th></tr>{route_rows}</table></div>
<div class="card"><h2>Safe environment</h2><table><tr><th>Variable</th><th>Value</th></tr>{env_rows}</table></div>
</main></body></html>"""
    return Response(page, mimetype="text/html")


@debug_bp.route("/debug/routes", methods=["GET"])
def debug_routes():
    if not _authorized():
        return _denied()
    return jsonify({"ok": True, "routes": _route_data()})


@debug_bp.route("/debug/health", methods=["GET"])
def debug_health():
    if not _authorized():
        return _denied()
    result: dict[str, Any] = {"ok": True, "service": "exam-os", "time_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        db = current_app.extensions.get("sqlalchemy")
        if db is not None:
            with db.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            result["database"] = "ok"
        else:
            result["database"] = "not_configured"
    except Exception as exc:
        result["ok"] = False
        result["database"] = f"error: {type(exc).__name__}: {exc}"
    return jsonify(result), (200 if result["ok"] else 503)


def install_debug_handlers(app) -> None:
    """Install JSON error pages so unexpected exceptions are visible in /debug/errors."""
    app.config.setdefault("DEBUG_PANEL_ERRORS", [])

    @app.errorhandler(Exception)
    def _debug_exception(error):
        # Keep normal 404/405 responses normal; only capture real exceptions.
        if isinstance(error, HTTPException):
            return error

        item = {
            "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "type": type(error).__name__,
            "message": str(error),
            "path": request.path,
            "method": request.method,
            "traceback": traceback.format_exc(),
        }
        errors = app.config["DEBUG_PANEL_ERRORS"]
        errors.append(item)
        del errors[:-20]
        app.logger.exception("Unhandled request error: %s %s", request.method, request.path)
        return jsonify({
            "ok": False,
            "error": type(error).__name__,
            "message": str(error),
            "path": request.path,
            "hint": "Open /debug to inspect registered routes and /debug/errors for recent exceptions.",
        }), 500

    @app.route("/debug/errors", methods=["GET"])
    def debug_errors():
        if not _authorized():
            return _denied()
        return jsonify({"ok": True, "errors": app.config["DEBUG_PANEL_ERRORS"]})
