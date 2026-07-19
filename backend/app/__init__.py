"""Exam OS Flask application factory.

Constructs the WSGI application used by the CBT API:

    * Binds shared extensions (SQLAlchemy, JWT, Migrate)
    * Registers versioned JSON API blueprints under ``/api/*``
    * Installs security headers, safe error handlers, and JWT loaders
    * Ensures schema + demo seed exist for zero-config local boot

Public contract (stable):
    create_app(config_class=Config) -> Flask

Do not rename blueprint URL prefixes or the ``/api/health`` payload shape —
the Vite frontend and ops probes depend on them.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Optional, Type, Union

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from app.config import Config
from app.extensions import db, jwt, migrate

logger = logging.getLogger("exam_os.app")

# Stable product identity returned by the health probe (do not change keys)
_SERVICE_NAME = "Exam OS"
_SERVICE_VERSION = "1.1.0"

# CORS methods / headers explicitly allowlisted — never reflect request headers
_CORS_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_CORS_ALLOW_HEADERS = ("Content-Type", "Authorization")
_CORS_EXPOSE_HEADERS = ("Content-Type", "X-Request-ID")
_CORS_MAX_AGE = 600

# Security headers applied to every response (API + error paths)
_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    # API serves JSON only — deny legacy MIME sniffing / XSS vectors
    ("X-XSS-Protection", "0"),
    ("Cache-Control", "no-store"),
)


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _safe_error_message(exc: BaseException, fallback: str) -> str:
    """
    Produce a client-safe error message.

    Never forward raw interpreter / SQLAlchemy / stack text to API clients
    during a live examination session.
    """
    if isinstance(exc, HTTPException):
        desc = getattr(exc, "description", None)
        if isinstance(desc, str) and desc and len(desc) <= 300:
            # Werkzeug descriptions are generally safe; still strip control chars
            cleaned = "".join(ch for ch in desc if ch >= " " or ch in "\t\n")
            return cleaned.strip() or fallback
    return fallback


def _json_error(error: str, message: str, status: int):
    """Uniform JSON error envelope used across HTTP + JWT handlers."""
    response = jsonify({"error": error, "message": message})
    response.status_code = status
    return response


def _ensure_upload_folder(path: str) -> None:
    """Create the upload directory if needed; never follow unsafe relative CWD surprises."""
    if not path or not isinstance(path, str):
        logger.warning("UPLOAD_FOLDER missing or invalid — uploads may fail")
        return
    if "\x00" in path:
        logger.error("UPLOAD_FOLDER contains null byte — refusing to create")
        return
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        # Boot must continue for read-only demo environments; log for operators
        logger.error("Could not create UPLOAD_FOLDER %s: %s", path, type(exc).__name__)


def _register_extensions(app: Flask) -> None:
    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)


def _register_cors(app: Flask) -> None:
    origins = app.config.get("CORS_ORIGINS") or []
    # flask-cors accepts a list; empty list would reflect nothing (safe fail-closed)
    if not origins:
        logger.warning("CORS_ORIGINS empty — browser clients will be blocked cross-origin")

    CORS(
        app,
        resources={r"/api/*": {"origins": origins}},
        supports_credentials=True,
        allow_headers=list(_CORS_ALLOW_HEADERS),
        expose_headers=list(_CORS_EXPOSE_HEADERS),
        methods=list(_CORS_METHODS),
        max_age=_CORS_MAX_AGE,
        # Do not send Access-Control-Allow-Origin: * when credentials are true
        send_wildcard=False,
        always_send=True,
    )


def _register_blueprints(app: Flask) -> None:
    """Import and mount API blueprints. Imports are deferred to avoid cycles."""
    from app.routes.admin import admin_bp
    from app.routes.analytics import analytics_bp
    from app.routes.attempts import attempts_bp
    from app.routes.auth import auth_bp
    from app.routes.banks import banks_bp
    from app.routes.chapters import chapters_bp
    from app.routes.exams import exams_bp
    from app.routes.imports import imports_bp
    from app.routes.platform import platform_bp
    from app.routes.notifications import notifications_bp
    from app.routes.questions import questions_bp
    from app.routes.subjects import subjects_bp

    # URL prefixes are part of the public API contract — do not change
    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(subjects_bp, url_prefix="/api/subjects")
    app.register_blueprint(chapters_bp, url_prefix="/api/chapters")
    app.register_blueprint(questions_bp, url_prefix="/api/questions")
    app.register_blueprint(exams_bp, url_prefix="/api/exams")
    app.register_blueprint(attempts_bp, url_prefix="/api/attempts")
    app.register_blueprint(analytics_bp, url_prefix="/api/analytics")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(imports_bp, url_prefix="/api/imports")
    app.register_blueprint(banks_bp, url_prefix="/api/banks")
    app.register_blueprint(platform_bp, url_prefix="/api/platform")
    app.register_blueprint(notifications_bp, url_prefix="/api/notifications")

    # --------------------------------------------
    # EXTENSION POINT: Register additional blueprints here
    # --------------------------------------------


def _register_request_hooks(app: Flask) -> None:
    """Request-ID, security headers, and session hygiene for CBT traffic."""

    @app.before_request
    def _bind_request_context() -> None:
        # Propagate or mint a request id for log correlation (exam incident response)
        incoming = request.headers.get("X-Request-ID", "")
        if (
            incoming
            and len(incoming) <= 64
            and all(c.isalnum() or c in "-_" for c in incoming)
        ):
            g.request_id = incoming
        else:
            g.request_id = secrets.token_hex(16)

        # Reject obviously hostile oversized query strings early (cheap DOS guard)
        if len(request.query_string) > 4096:
            return _json_error("Bad request", "Query string too long", 400)

        # Maintenance / emergency gate (configuration-driven)
        try:
            from app.services.maintenance_engine import check_request_allowed
            from app.models.user import User
            from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity

            user = None
            try:
                verify_jwt_in_request(optional=True)
                uid = get_jwt_identity()
                if uid is not None:
                    user = User.query.get(int(uid))
            except Exception:
                user = None
            ok, msg = check_request_allowed(request.path, request.method, user)
            if not ok:
                return _json_error("Service unavailable", msg or "Maintenance mode", 503)
        except Exception:
            logger.debug("maintenance check skipped", exc_info=True)

    @app.after_request
    def _apply_security_headers(response):
        for header, value in _SECURITY_HEADERS:
            response.headers.setdefault(header, value)

        # Request correlation for clients / reverse proxies
        req_id = getattr(g, "request_id", None)
        if req_id:
            response.headers.setdefault("X-Request-ID", req_id)

        logger.info(
            "backend %s %s -> %s req_id=%s",
            request.method,
            request.path,
            response.status_code,
            req_id,
        )

        # JSON APIs should not be framed or cached by shared proxies mid-exam
        if request.path.startswith("/api/"):
            response.headers.setdefault("Content-Type", "application/json; charset=utf-8")
            # HSTS only when the request was HTTPS (avoid breaking local http:// dev)
            if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
                response.headers.setdefault(
                    "Strict-Transport-Security",
                    "max-age=31536000; includeSubDomains",
                )

        return response

    @app.teardown_appcontext
    def _shutdown_session(exc: Optional[BaseException] = None) -> None:
        """
        Return the DB connection to the pool at the end of each app context.

        On error, roll back so a failed attempt-save cannot leave an open
        transaction that poisons the next request on the same connection.
        """
        try:
            if exc is not None:
                db.session.rollback()
        except Exception:  # noqa: BLE001 — teardown must not raise
            logger.exception("Rollback during teardown failed")
        try:
            db.session.remove()
        except Exception:  # noqa: BLE001
            logger.exception("session.remove() during teardown failed")


def _register_error_handlers(app: Flask) -> None:
    """HTTP + generic handlers that never leak stack traces to exam clients."""

    @app.errorhandler(400)
    def bad_request(e):
        return _json_error("Bad request", _safe_error_message(e, "Bad request"), 400)

    @app.errorhandler(401)
    def unauthorized(e):
        return _json_error("Unauthorized", "Authentication required", 401)

    @app.errorhandler(403)
    def forbidden(e):
        return _json_error("Forbidden", "Insufficient permissions", 403)

    @app.errorhandler(404)
    def not_found(e):
        return _json_error("Not found", _safe_error_message(e, "Resource not found"), 404)

    @app.errorhandler(405)
    def method_not_allowed(e):
        return _json_error("Method not allowed", "HTTP method not allowed for this endpoint", 405)

    @app.errorhandler(413)
    def payload_too_large(e):
        return _json_error("Payload too large", "Request body exceeds size limit", 413)

    @app.errorhandler(415)
    def unsupported_media_type(e):
        return _json_error("Unsupported media type", "Content-Type not supported", 415)

    @app.errorhandler(429)
    def too_many_requests(e):
        return _json_error("Too many requests", "Rate limit exceeded — try again later", 429)

    @app.errorhandler(500)
    def internal_error(e):
        logger.exception(
            "Internal server error request_id=%s path=%s",
            getattr(g, "request_id", "-"),
            getattr(request, "path", "-"),
        )
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return _json_error("Internal server error", "Something went wrong", 500)

    @app.errorhandler(HTTPException)
    def handle_http_exception(e: HTTPException):
        """Catch-all for Werkzeug HTTP errors not listed above."""
        status = int(e.code or 500)
        if status >= 500:
            logger.exception("HTTP %s request_id=%s", status, getattr(g, "request_id", "-"))
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return _json_error("Internal server error", "Something went wrong", status)
        # Map common codes to stable error labels used by the frontend
        label = {
            400: "Bad request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not found",
            405: "Method not allowed",
            409: "Conflict",
            413: "Payload too large",
            415: "Unsupported media type",
            422: "Unprocessable entity",
            429: "Too many requests",
        }.get(status, e.name or "Error")
        msg_fallback = {
            401: "Authentication required",
            403: "Insufficient permissions",
        }.get(status, e.name or "Request failed")
        return _json_error(label, _safe_error_message(e, msg_fallback), status)

    @app.errorhandler(Exception)
    def handle_unexpected(e: Exception):
        """Last-resort handler — log server-side, generic client body."""
        if isinstance(e, HTTPException):
            return handle_http_exception(e)
        logger.exception(
            "Unhandled exception request_id=%s path=%s",
            getattr(g, "request_id", "-"),
            getattr(request, "path", "-"),
        )
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return _json_error("Internal server error", "Something went wrong", 500)


def _register_jwt_loaders(app: Flask) -> None:
    """
    JWT error callbacks with stable JSON shapes.

    Intentionally do **not** echo library error strings to clients (token
    oracle / info leak). Signatures match Flask-JWT-Extended expectations.
    """

    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_payload):  # noqa: ARG001
        return _json_error("Token expired", "Please login again", 401)

    @jwt.invalid_token_loader
    def invalid_token_callback(error):  # noqa: ARG001
        # `error` may contain parse details — never return it verbatim
        logger.info(
            "Invalid JWT rejected request_id=%s",
            getattr(g, "request_id", "-"),
        )
        return _json_error("Invalid token", "Token is invalid or malformed", 401)

    @jwt.unauthorized_loader
    def missing_token_callback(error):  # noqa: ARG001
        return _json_error("Authorization required", "Missing or invalid authorization header", 401)

    @jwt.revoked_token_loader
    def revoked_token_callback(jwt_header, jwt_payload):  # noqa: ARG001
        return _json_error("Invalid token", "Token has been revoked", 401)

    @jwt.needs_fresh_token_loader
    def needs_fresh_token_callback(jwt_header, jwt_payload):  # noqa: ARG001
        return _json_error("Authorization required", "Fresh login required", 401)


def _register_health_routes(app: Flask) -> None:
    @app.route("/api/health")
    def health():
        """
        Liveness probe — process is up and routing works.

        Payload keys/values kept stable for existing monitors and README checks.
        Does not touch the database (avoid false downs during brief DB blips
        while students are mid-exam on cached connections).
        """
        return jsonify(
            {
                "status": "ok",
                "service": _SERVICE_NAME,
                "version": _SERVICE_VERSION,
            }
        )


def _bootstrap_database(app: Flask) -> None:
    """
    Register models, create missing tables, and seed demo data when empty.

    Failures are logged and re-raised — a CBT API without a schema must not
    silently serve 500s on every route. Seed is idempotent (see seed_database).
    """
    skip_seed = _truthy_env("EXAM_OS_SKIP_SEED", default=False)
    skip_create = _truthy_env("EXAM_OS_SKIP_CREATE_ALL", default=False)

    with app.app_context():
        # Import models so metadata is registered on db.Model.metadata
        from app import models  # noqa: F401

        if not skip_create:
            try:
                db.create_all()
            except Exception:
                logger.exception("db.create_all() failed during application bootstrap")
                raise
            try:
                from app.services.schema_upgrade import ensure_additive_schema

                ensure_additive_schema()
            except Exception:
                logger.exception("ensure_additive_schema failed")
                # Non-fatal for brand-new DBs where create_all already applied models

        if not skip_seed:
            try:
                from app.services.seed import seed_database

                seed_database()
            except Exception:
                logger.exception("seed_database() failed during application bootstrap")
                raise
        try:
            from app.services.notification_engine import ensure_default_templates

            ensure_default_templates()
        except Exception:
            logger.debug("notification templates seed skipped", exc_info=True)


def create_app(config_class: Union[Type[Any], Any] = Config) -> Flask:
    """
    Application factory.

    Parameters
    ----------
    config_class:
        Object or class passed to ``app.config.from_object``. Defaults to
        ``app.config.Config``. Tests may pass an alternate object with the
        same attribute names.
    """
    # Load .env once per process factory call (does not override real env by default)
    load_dotenv()

    app = Flask(__name__)
    app.config.from_object(config_class)

    # Harden Flask runtime defaults without breaking existing config keys
    app.config.setdefault("JSON_SORT_KEYS", False)
    # Never expose interactive debugger via mis-set config in multi-worker hosts
    if os.getenv("FLASK_ENV", "").strip().lower() == "production":
        app.config["DEBUG"] = False
        app.config["TESTING"] = app.config.get("TESTING", False)
        app.config["PROPAGATE_EXCEPTIONS"] = False

    _register_extensions(app)
    _register_cors(app)
    _ensure_upload_folder(str(app.config.get("UPLOAD_FOLDER") or ""))
    _register_blueprints(app)
    _register_request_hooks(app)
    _register_error_handlers(app)
    _register_jwt_loaders(app)
    _register_health_routes(app)
    _bootstrap_database(app)

    logger.info(
        "Exam OS application created (debug=%s testing=%s)",
        bool(app.debug),
        bool(app.testing),
    )

    return app


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Rate limiting (Flask-Limiter) on /api/auth/login and attempt submit
# - Structured JSON logging middleware + OpenTelemetry tracing spans
# - /api/health/ready deep check (DB SELECT 1) separate from liveness
# - ProxyFix for multi-hop TLS termination (needs trusted-proxy config)
# - CSRF double-submit if JWT ever moves to cookies
# - Blueprint-level registration via entry points / plugin loader
# --------------------------------------------
