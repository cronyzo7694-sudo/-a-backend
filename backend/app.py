"""Exam OS — Flask WSGI entrypoint.

This module is the process boundary for the CBT API. It must remain a thin,
safe bootstrap: construct the application via the factory, expose the WSGI
callable, and (when executed as a script) run the development server with
hardened defaults suitable for local exam-engine work.

Run (development)::

    python app.py

WSGI (production — preferred)::

    gunicorn -b 0.0.0.0:5000 --workers 2 --threads 4 "app:app"

Public contract (do not break):
    * Module-level ``app`` — Flask application instance (WSGI callable)
    * ``python app.py`` — starts the development server
    * Factory import path remains ``from app import create_app``

Environment (optional):
    PORT              Listen port (default: 5000). Invalid values fall back safely.
    HOST              Bind address (default: 0.0.0.0).
    FLASK_ENV         development | production | testing (legacy Flask flag)
    FLASK_DEBUG       1/true/yes to enable debugger (overrides FLASK_ENV when set)
    FLASK_USE_RELOADER  0/false to disable the Werkzeug reloader even in debug
    EXAM_OS_THREADED  1/true to enable threaded dev server (default: on)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final

# ---------------------------------------------------------------------------
# Constants — keep defaults stable for existing scripts and docs
# ---------------------------------------------------------------------------
DEFAULT_HOST: Final[str] = "0.0.0.0"
DEFAULT_PORT: Final[int] = 5000
MIN_PORT: Final[int] = 1
MAX_PORT: Final[int] = 65535
LOG_FORMAT: Final[str] = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
LOG_DATEFMT: Final[str] = "%Y-%m-%d %H:%M:%S"

# Truthy tokens accepted for boolean env flags (case-insensitive)
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on", "y"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", "n", ""})

logger = logging.getLogger("exam_os.entrypoint")


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable without raising."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    # Unknown token — fail closed to the safer default
    return default


def _parse_port(raw: str | None, default: int = DEFAULT_PORT) -> int:
    """
    Parse TCP port from env.

    Rejects non-integers, out-of-range values, and oversized strings that could
    be used as a cheap input-amplification nuisance. Never raises to callers.
    """
    if raw is None:
        return default
    text = raw.strip()
    # Bound length to avoid pathological env values
    if not text or len(text) > 5 or not text.isdigit():
        logger.warning(
            "Invalid PORT value rejected; using default %s", default
        )
        return default
    try:
        port = int(text)
    except (TypeError, ValueError):
        logger.warning(
            "Unparseable PORT value rejected; using default %s", default
        )
        return default
    if port < MIN_PORT or port > MAX_PORT:
        logger.warning(
            "PORT %s out of range %s-%s; using default %s",
            port,
            MIN_PORT,
            MAX_PORT,
            default,
        )
        return default
    return port


def _parse_host(raw: str | None, default: str = DEFAULT_HOST) -> str:
    """
    Parse bind host.

    Allows only a conservative character set (IPv4/IPv6/hostname-ish) to reduce
    header/injection-style surprises if HOST is injected via a compromised env
    file. Does not perform DNS — bind will fail later if the address is wrong.
    """
    if raw is None:
        return default
    host = raw.strip()
    if not host or len(host) > 253:
        logger.warning("Invalid HOST rejected; using default %s", default)
        return default
    # Allow alnum, dots, colons (IPv6), hyphens, percent (IPv6 zone), underscores
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:_[]%")
    if any(ch not in allowed for ch in host):
        logger.warning("HOST contains disallowed characters; using default")
        return default
    return host


def _resolve_debug() -> bool:
    """
    Resolve debug mode with explicit precedence:

        1. FLASK_DEBUG if set
        2. else FLASK_ENV == "development"
        3. else False (fail closed — never default debug on)

    Debug enables the interactive debugger and code reloader. That MUST never
    be on in a production CBT deployment (exposes stack traces / console).
    """
    if "FLASK_DEBUG" in os.environ:
        return _env_flag("FLASK_DEBUG", default=False)
    env = (os.getenv("FLASK_ENV") or "").strip().lower()
    return env == "development"


def _configure_logging(debug: bool) -> None:
    """
    Idempotent root logging setup for the process entrypoint.

    Does not reconfigure if handlers already exist (e.g. gunicorn --log-level),
    so production process managers keep control of log routing.
    """
    root = logging.getLogger()
    if root.handlers:
        # Respect host process configuration; only ensure our logger level
        logger.setLevel(logging.DEBUG if debug else logging.INFO)
        return

    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        stream=sys.stderr,
    )
    # Keep noisy Werkzeug access logs visible; silence nothing security-relevant
    logging.getLogger("werkzeug").setLevel(logging.INFO if not debug else logging.DEBUG)
    logger.setLevel(level)


def _create_application():
    """
    Build the Flask app through the factory.

    Import is deferred so that ``gunicorn app:app`` / module import failures
    surface as a clear bootstrap error rather than a half-initialized process.
    """
    # --------------------------------------------
    # EXTENSION POINT: preload secrets managers / APM agents before factory
    # --------------------------------------------
    from app import create_app

    return create_app()


# ---------------------------------------------------------------------------
# WSGI callable — imported by gunicorn/uwsgi and by this script
# ---------------------------------------------------------------------------
try:
    app = _create_application()
except Exception:
    # Last-resort stderr log without leaking secrets; re-raise so process exits
    logging.basicConfig(level=logging.ERROR, format=LOG_FORMAT, stream=sys.stderr)
    logging.getLogger("exam_os.entrypoint").exception(
        "Failed to create Exam OS application — check configuration and database"
    )
    raise


def main() -> int:
    """
    Development server entry.

    Returns a process exit code (0 success, 2 configuration error).
    Production deployments should not use this path — use a real WSGI server.
    """
    host = _parse_host(os.getenv("HOST"))
    port = _parse_port(os.getenv("PORT"))
    debug = _resolve_debug()
    use_reloader = _env_flag("FLASK_USE_RELOADER", default=debug)
    threaded = _env_flag("EXAM_OS_THREADED", default=True)

    _configure_logging(debug)

    # Hard safety: interactive debugger must never face untrusted networks
    # without an explicit override. CBT platforms handle exam credentials —
    # a public debug console is a critical incident.
    if debug and host in ("0.0.0.0", "::", "[::]"):
        allow_public_debug = _env_flag("EXAM_OS_ALLOW_PUBLIC_DEBUG", default=False)
        if not allow_public_debug:
            logger.warning(
                "Debug mode requested while binding %s — forcing debug=False "
                "for safety. Set HOST=127.0.0.1 for local debug, or set "
                "EXAM_OS_ALLOW_PUBLIC_DEBUG=1 only on trusted networks.",
                host,
            )
            debug = False
            use_reloader = False

    if debug:
        logger.warning(
            "Exam OS development server starting with DEBUG enabled "
            "(host=%s port=%s). Do not use this mode for live examinations.",
            host,
            port,
        )
    else:
        logger.info(
            "Exam OS development server starting (host=%s port=%s debug=False)",
            host,
            port,
        )

    # --------------------------------------------
    # FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
    # Wire graceful SIGTERM handling + connection drain for blue/green deploys
    # inside a custom WSGI runner. Prefer gunicorn's worker lifecycle instead.
    # --------------------------------------------

    try:
        # Flask/Werkzeug dev server — adequate for local CBT QA only
        app.run(
            host=host,
            port=port,
            debug=debug,
            use_reloader=use_reloader,
            threaded=threaded,
            # load_dotenv already handled inside create_app; do not double-load
        )
    except OSError as exc:
        # Bind failures (EADDRINUSE, permission on low ports) — no stack to clients
        logger.error("Failed to bind server on %s:%s — %s", host, port, exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Exam OS development server stopped by operator")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
