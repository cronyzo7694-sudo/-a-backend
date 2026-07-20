"""Application configuration for Exam OS (CBT platform).

Responsibilities:
    * Resolve database connectivity (SQLite default ↔ Neon/PostgreSQL optional)
    * Centralize secrets, JWT lifetimes, CORS, uploads, and feature flags
    * Fail soft on malformed environment values (never crash the exam API
      because an operator mistyped MAX_CONTENT_LENGTH)

Loaded by the application factory via::

    app.config.from_object(Config)

Public surface (stable — do not rename):
    Config                          Main configuration class
    _resolve_database_url()         DB URL resolver (used at import / tests)

Environment variables (optional):
    SECRET_KEY, JWT_SECRET_KEY
    DATABASE_URL, NEON_DATABASE_URL
    CORS_ORIGINS
    UPLOAD_FOLDER, MAX_CONTENT_LENGTH
    JWT_ACCESS_HOURS, JWT_REFRESH_DAYS   (optional lifetime overrides)
    FLASK_ENV                            (production hardening hints only)

--------------------------------------------
EXTENSION POINT: Add feature flags / config keys on Config below.
--------------------------------------------
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Final, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("exam_os.config")

# ---------------------------------------------------------------------------
# Paths & defaults (stable)
# ---------------------------------------------------------------------------
_BACKEND_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_DEFAULT_SQLITE_PATH: Final[Path] = _BACKEND_ROOT / "exam_os.db"
_DEFAULT_UPLOAD_DIR: Final[Path] = _BACKEND_ROOT / "uploads"

_DEFAULT_SECRET_KEY: Final[str] = "exam-os-dev-secret-change-in-production"
_DEFAULT_JWT_SECRET_KEY: Final[str] = "exam-os-jwt-secret-change-in-production"

_DEFAULT_CORS: Final[str] = (
    "http://localhost:5173,http://127.0.0.1:5173,"
    "https://exam-os-frontend-azure.vercel.app,"
    "https://exam-os-frontend.vercel.app,"
    "https://exam-os-frontend-lxem7xtya-cronyzo.vercel.app,"
    "https://exam-os-frontend-4kit4s83e-cronyzo.vercel.app,"
    "https://pariksha.cronyzo7694.workers.dev,"
    "https://exam-os-frontend-azure.vercel.app,"
    "https://exam-os-frontend.pages.dev"
)
_DEFAULT_MAX_UPLOAD_BYTES: Final[int] = 16 * 1024 * 1024  # 16 MiB
_MIN_UPLOAD_BYTES: Final[int] = 1024  # 1 KiB floor — reject absurd zeros
_MAX_UPLOAD_BYTES_CAP: Final[int] = 100 * 1024 * 1024  # 100 MiB hard ceiling

_DEFAULT_ACCESS_HOURS: Final[int] = 12
_DEFAULT_REFRESH_DAYS: Final[int] = 30

# Postgres URL schemes accepted from env (SQLAlchemy 1.4/2.x + Neon)
_POSTGRES_PREFIXES: Final[Tuple[str, ...]] = (
    "postgresql://",
    "postgres://",
    "postgresql+psycopg2://",
    "postgresql+psycopg://",
    "postgresql+psycopg2cffi://",
)

# CORS origin: scheme://host[:port] only — blocks header-injection junk in env
_ORIGIN_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://[A-Za-z0-9][A-Za-z0-9.\-]*(:\d{1,5})?$",
    re.IGNORECASE,
)


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return stripped env value or default; empty string → default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _env_int(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """
    Parse integer env vars without raising.

    Malformed values, overflows, and out-of-range numbers fall back to default
    so a bad deploy env cannot take down the CBT API process at import time.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    text = raw.strip()
    # Bound length — reject pathological strings (DOS via env in shared hosts)
    if not text or len(text) > 16:
        logger.warning("Config %s invalid length; using default %s", name, default)
        return default
    try:
        value = int(text, 10)
    except ValueError:
        logger.warning("Config %s=%r not an integer; using default %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        logger.warning(
            "Config %s=%s below minimum %s; using default %s",
            name,
            value,
            minimum,
            default,
        )
        return default
    if maximum is not None and value > maximum:
        logger.warning(
            "Config %s=%s above maximum %s; using default %s",
            name,
            value,
            maximum,
            default,
        )
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _normalize_postgres_url(url: str) -> str:
    """
    Normalize operator-supplied Postgres URLs for SQLAlchemy.

    * ``postgres://`` → ``postgresql://`` (SQLAlchemy requirement)
    * Strip surrounding whitespace / accidental quotes from secret managers
    """
    cleaned = url.strip().strip('"').strip("'")
    if cleaned.startswith("postgres://"):
        cleaned = "postgresql://" + cleaned[len("postgres://") :]
    return cleaned


def _is_postgres_url(url: str) -> bool:
    lower = url.strip().lower()
    return any(lower.startswith(prefix) for prefix in _POSTGRES_PREFIXES)


def _is_sqlite_url(url: str) -> bool:
    return url.strip().lower().startswith("sqlite:")


def _resolve_database_url() -> str:
    """
    Prefer DATABASE_URL / NEON_DATABASE_URL when set (PostgreSQL/Neon).

    Fall back to local SQLite so the project runs out of the box.

    Resolution order:
        1. NEON_DATABASE_URL (explicit Neon)
        2. DATABASE_URL (generic — postgres or sqlite)
        3. SQLite file at backend/exam_os.db

    Invalid non-empty URLs that are neither Postgres nor SQLite are rejected
    with a warning and SQLite fallback — avoids boot loops on typos that would
    otherwise hand SQLAlchemy an unusable scheme during a live exam window.
    """
    candidate = _env_str("NEON_DATABASE_URL") or _env_str("DATABASE_URL")

    if candidate:
        if _is_postgres_url(candidate):
            return _normalize_postgres_url(candidate)
        if _is_sqlite_url(candidate):
            # Honor explicit sqlite URL (tests / alternate file path)
            return candidate.strip().strip('"').strip("'")
        logger.warning(
            "DATABASE_URL/NEON_DATABASE_URL scheme not recognized; "
            "falling back to local SQLite. Value starts with: %r",
            candidate[:32],
        )

    # Default: SQLite next to the backend package (absolute path — CWD-safe)
    sqlite_path = _DEFAULT_SQLITE_PATH.resolve()
    # SQLAlchemy accepts three slashes + absolute path on POSIX
    return f"sqlite:///{sqlite_path.as_posix()}"


def _parse_cors_origins(raw: Optional[str]) -> List[str]:
    """
    Parse and validate CORS origin list.

    Only http(s) origins with a sane host:port shape are kept. This prevents
    accidental injection of arbitrary strings into Access-Control-Allow-Origin
    via a compromised or hand-edited environment file.
    """
    text = raw if raw is not None else _DEFAULT_CORS
    origins: List[str] = []
    seen = set()
    for origin in (
        "https://exam-os-frontend-azure.vercel.app",
        "https://exam-os-frontend.vercel.app",
        "https://pariksha.cronyzo7694.workers.dev",
        "https://exam-os-frontend-azure.vercel.app",
    ):
        if origin not in seen:
            seen.add(origin.lower())
            origins.append(origin)
    for part in text.split(","):
        origin = part.strip().rstrip("/")
        if not origin:
            continue
        if len(origin) > 253:
            logger.warning("CORS origin skipped (too long)")
            continue
        if not _ORIGIN_RE.match(origin):
            # Allow localhost with uncommon ports already covered by regex;
            # reject javascript:, data:, null, wildcard spoof attempts, etc.
            logger.warning("CORS origin rejected as unsafe: %r", origin[:64])
            continue
        # Extra parse guard
        try:
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                logger.warning("CORS origin rejected (credentials/query/fragment): %r", origin[:64])
                continue
        except Exception:  # noqa: BLE001
            continue
        key = origin.lower()
        if key not in seen:
            seen.add(key)
            origins.append(origin)

    if not origins:
        # Never return an empty allow-list silently — fall back to dev defaults
        logger.warning("No valid CORS_ORIGINS; using development defaults")
        return [o.strip() for o in _DEFAULT_CORS.split(",") if o.strip()]
    return origins


def _resolve_upload_folder(raw: Optional[str]) -> str:
    """
    Resolve upload directory to a normalized absolute path.

    Guards:
        * Empty → default backend/uploads
        * Relative paths resolved against backend root (not process CWD)
        * Null bytes rejected
        * Result is absolute and normalized (no trailing slash variance)
    """
    if not raw or not raw.strip():
        path = _DEFAULT_UPLOAD_DIR
    else:
        text = raw.strip()
        if "\x00" in text:
            logger.warning("UPLOAD_FOLDER contained null byte; using default")
            path = _DEFAULT_UPLOAD_DIR
        else:
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                candidate = _BACKEND_ROOT / candidate
            try:
                path = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                logger.warning("UPLOAD_FOLDER could not be resolved; using default")
                path = _DEFAULT_UPLOAD_DIR

    return str(path)


def _build_engine_options(database_uri: str) -> dict:
    """
    Engine options tuned for CBT workloads.

    * pool_pre_ping: drop stale connections (Neon idle disconnects, proxies)
    * pool_recycle: avoid server-side idle timeouts on Postgres
    * SQLite: no QueuePool sizing (StaticPool/NullPool semantics via SA defaults
      when appropriate); keep options minimal for maximum compatibility
    """
    options: dict = {
        "pool_pre_ping": True,
    }

    if _is_postgres_url(database_uri) or database_uri.startswith("postgresql"):
        # Conservative pool for exam API — concurrent students saving answers
        options["pool_recycle"] = _env_int(
            "DB_POOL_RECYCLE",
            280,
            minimum=30,
            maximum=3600,
        )
        # Only set size knobs when explicitly provided (avoid surprising SA)
        pool_size = os.getenv("DB_POOL_SIZE")
        if pool_size is not None:
            options["pool_size"] = _env_int("DB_POOL_SIZE", 5, minimum=1, maximum=50)
        max_overflow = os.getenv("DB_MAX_OVERFLOW")
        if max_overflow is not None:
            options["max_overflow"] = _env_int("DB_MAX_OVERFLOW", 10, minimum=0, maximum=100)
    elif _is_sqlite_url(database_uri):
        # check_same_thread left default — Flask-SQLAlchemy scoped sessions
        # already serialize access per request context in typical deployments.
        options["connect_args"] = {
            # Wait when the DB is briefly locked (concurrent answer saves)
            "timeout": _env_int("SQLITE_TIMEOUT", 30, minimum=1, maximum=120),
        }

    return options


def _warn_if_insecure_secrets(secret_key: str, jwt_secret: str) -> None:
    """
    Log (never raise) when production-like env still uses baked-in dev secrets.

    Raising would break out-of-the-box demo login; operators get a loud warning.
    """
    env = (_env_str("FLASK_ENV") or "").lower()
    using_defaults = (
        secret_key == _DEFAULT_SECRET_KEY
        or jwt_secret == _DEFAULT_JWT_SECRET_KEY
        or secret_key == jwt_secret
    )
    if not using_defaults:
        # Soft strength check without blocking startup
        if len(secret_key) < 16 or len(jwt_secret) < 16:
            logger.warning(
                "SECRET_KEY / JWT_SECRET_KEY shorter than 16 characters — "
                "use long random values for live examinations"
            )
        return

    if env == "production":
        logger.error(
            "CRITICAL: default SECRET_KEY/JWT_SECRET_KEY in production — "
            "set strong unique secrets before any live CBT session"
        )
    else:
        logger.warning(
            "Using built-in development SECRET_KEY/JWT_SECRET_KEY. "
            "Override via environment before deploying Exam OS."
        )


class Config:
    """Default configuration object consumed by ``create_app``."""

    # ----- Secrets & sessions -------------------------------------------------
    SECRET_KEY = _env_str("SECRET_KEY", _DEFAULT_SECRET_KEY) or _DEFAULT_SECRET_KEY
    JWT_SECRET_KEY = (
        _env_str("JWT_SECRET_KEY", _DEFAULT_JWT_SECRET_KEY) or _DEFAULT_JWT_SECRET_KEY
    )

    # Prefer explicit hours/days env overrides when present; else stable defaults
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(
        hours=_env_int("JWT_ACCESS_HOURS", _DEFAULT_ACCESS_HOURS, minimum=1, maximum=168)
    )
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(
        days=_env_int("JWT_REFRESH_DAYS", _DEFAULT_REFRESH_DAYS, minimum=1, maximum=365)
    )

    # Bearer-header JWT remains the sole location used by the existing frontend.
    # Do not enable cookies here without CSRF coordination in the API layer.
    JWT_TOKEN_LOCATION = ["headers"]
    JWT_HEADER_NAME = "Authorization"
    JWT_HEADER_TYPE = "Bearer"
    # Algorithm explicit — prevents accidental none/alg confusion if library defaults shift
    JWT_ALGORITHM = "HS256"
    JWT_DECODE_ALGORITHMS = ["HS256"]

    # ----- Database -----------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = _resolve_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = _build_engine_options(SQLALCHEMY_DATABASE_URI)
    # Echo stays off — never log SQL with PII/answers in exam production
    SQLALCHEMY_ECHO = _env_bool("SQLALCHEMY_ECHO", False)

    # ----- CORS ---------------------------------------------------------------
    CORS_ORIGINS = _parse_cors_origins(_env_str("CORS_ORIGINS", _DEFAULT_CORS))

    # ----- Uploads (question media / CSV import size bound) -------------------
    UPLOAD_FOLDER = _resolve_upload_folder(_env_str("UPLOAD_FOLDER"))
    MAX_CONTENT_LENGTH = _env_int(
        "MAX_CONTENT_LENGTH",
        _DEFAULT_MAX_UPLOAD_BYTES,
        minimum=_MIN_UPLOAD_BYTES,
        maximum=_MAX_UPLOAD_BYTES_CAP,
    )

    # ----- JSON / response hygiene --------------------------------------------
    # Preserve insertion order; avoid wasted sort work on large exam payloads
    JSON_SORT_KEYS = False

    # ----- CBT feature flags (legacy keys — Platform Config Engine is preferred) -
    # --------------------------------------------
    # EXTENSION POINT: Add feature flags / config keys here
    # Full flag set lives in app.services.config_engine.DEFAULT_PLATFORM_CONFIG
    # and is overridable via ENABLE_* env vars or exam_os.config.json
    # --------------------------------------------
    FEATURE_NEGATIVE_MARKING = _env_bool("FEATURE_NEGATIVE_MARKING", True)
    FEATURE_SECTION_LOCK = _env_bool("FEATURE_SECTION_LOCK", True)
    DEFAULT_PAGE_SIZE = _env_int("DEFAULT_PAGE_SIZE", 20, minimum=1, maximum=100)
    MAX_PAGE_SIZE = _env_int("MAX_PAGE_SIZE", 100, minimum=1, maximum=500)

    # Application identity (also in Configuration Engine)
    APP_NAME = _env_str("EXAM_OS_APP_NAME", "परीक्षa") or "परीक्षa"
    APP_VERSION = "1.0.0"

    # Ensure DEFAULT_PAGE_SIZE never exceeds MAX_PAGE_SIZE after env overrides
    if DEFAULT_PAGE_SIZE > MAX_PAGE_SIZE:
        DEFAULT_PAGE_SIZE = MAX_PAGE_SIZE


# Import-time operator guidance (no exceptions — app must still boot for demos)
_warn_if_insecure_secrets(Config.SECRET_KEY, Config.JWT_SECRET_KEY)

# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Split Config into DevelopmentConfig / ProductionConfig / TestingConfig
#   selected in create_app (requires factory changes).
# - Enforce non-default secrets when FLASK_ENV=production via hard fail
#   (would break deploys that rely on current soft defaults until secrets set).
# - Integrate cloud secret managers (AWS SM / GCP SM) for SECRET_KEY rotation.
# - Per-bind SQLALCHEMY_BINDS for read replicas on analytics queries.
# --------------------------------------------
