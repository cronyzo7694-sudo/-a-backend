"""Alembic environment for Exam OS (Flask-Migrate).

Resolves the database URL and MetaData exclusively from the Flask application
config so SQLite (default) and Neon/PostgreSQL (optional) stay in lockstep with
runtime. Invoked by::

    flask db upgrade | migrate | downgrade
    alembic ...   # when Flask app context is already established by Flask-Migrate

Public callables retained for compatibility with Flask-Migrate tooling:
    get_engine, get_engine_url, get_metadata,
    run_migrations_offline, run_migrations_online

Security notes:
    * Connection credentials are never written to logs.
    * ConfigParser percent-interpolation is neutralized when setting sqlalchemy.url.
    * fileConfig is skipped safely when alembic.ini is missing or incomplete.
    * Migration connection is closed promptly; failures do not leak secrets.
"""

from __future__ import annotations

import logging
import re
from logging.config import fileConfig
from typing import Any, Mapping, MutableMapping, Optional

from alembic import context
from flask import current_app

# ---------------------------------------------------------------------------
# Alembic Config object — provides access to alembic.ini values
# ---------------------------------------------------------------------------
config = context.config

logger = logging.getLogger("alembic.env")

# Characters that ConfigParser treats as interpolation markers.
# Doubling "%" is required before config.set_main_option() or Alembic crashes
# on URLs that contain percent-encoded query params (common with Neon SSL URLs).
_PERCENT_RE = re.compile(r"%")


def _configure_alembic_logging() -> None:
    """
    Load logging from alembic.ini when available.

    Never raises: a missing/broken logging section must not block CBT schema
    migrations during incident recovery.
    """
    config_file = getattr(config, "config_file_name", None)
    if not config_file:
        return
    try:
        # disable_existing_loggers=False preserves Flask / app loggers
        fileConfig(config_file, disable_existing_loggers=False)
    except Exception as exc:  # noqa: BLE001 — bootstrap path; log and continue
        logging.getLogger("alembic.env").warning(
            "Alembic fileConfig skipped (%s); using existing logging configuration",
            type(exc).__name__,
        )


_configure_alembic_logging()


def _require_migrate_extension() -> Any:
    """Return the Flask-Migrate extension or raise a clear operator error."""
    try:
        ext = current_app.extensions.get("migrate")
    except RuntimeError as exc:
        raise RuntimeError(
            "Alembic env.py requires an active Flask application context. "
            "Use `flask db ...` (Flask-Migrate) rather than bare `alembic`."
        ) from exc
    if ext is None:
        raise RuntimeError(
            "Flask-Migrate is not initialized on the application "
            "(current_app.extensions['migrate'] is missing)."
        )
    return ext


def get_engine():
    """
    Resolve the SQLAlchemy Engine used by the running Flask app.

    Supports both legacy Flask-SQLAlchemy (``.get_engine()``) and newer
    versions that expose ``.engine`` directly.
    """
    db = _require_migrate_extension().db
    try:
        return db.get_engine()
    except (TypeError, AttributeError):
        return db.engine


def _mask_database_url(url: str) -> str:
    """
    Redact password material from a SQLAlchemy URL for safe log output.

    Handles forms like:
        dialect://user:password@host/db
        dialect+driver://user:password@host:port/db?q=1
    """
    if not url:
        return "<empty>"
    # Redact user:password@ → user:***@
    redacted = re.sub(
        r"(://[^:/?#\s]+):([^@/\s]+)@",
        r"\1:***@",
        url,
        count=1,
    )
    return redacted


def get_engine_url() -> str:
    """
    Return the database URL string for Alembic configuration.

    Percent signs are escaped for ConfigParser. The raw URL (including
    credentials) is returned to Alembic internals only — callers that log
    must use ``_mask_database_url``.
    """
    engine = get_engine()
    url_obj = getattr(engine, "url", None)
    try:
        # SQLAlchemy 1.4+/2.x URL object
        rendered = url_obj.render_as_string(hide_password=False)  # type: ignore[union-attr]
    except AttributeError:
        rendered = str(url_obj if url_obj is not None else engine)
    # Neutralize ConfigParser interpolation (Neon sslmode URLs often contain %)
    return _PERCENT_RE.sub("%%", rendered)


def _safe_set_sqlalchemy_url() -> None:
    """Push the app DB URL into Alembic config without crashing on % chars."""
    try:
        url = get_engine_url()
        config.set_main_option("sqlalchemy.url", url)
        logger.info(
            "Alembic target database: %s",
            _mask_database_url(url.replace("%%", "%")),
        )
    except Exception:
        logger.exception(
            "Failed to resolve database URL for migrations — "
            "check DATABASE_URL / SQLALCHEMY_DATABASE_URI"
        )
        raise


# Populate alembic config + bind target_db at import time (Flask-Migrate contract)
_safe_set_sqlalchemy_url()
target_db = _require_migrate_extension().db


def get_metadata():
    """
    Return SQLAlchemy MetaData for autogenerate.

    Flask-SQLAlchemy 3 multi-bind layout uses ``metadatas[None]`` for the
    default bind; older layouts expose a single ``metadata`` attribute.
    """
    if hasattr(target_db, "metadatas"):
        metadatas = target_db.metadatas
        # Prefer default bind; fall back to first available bind metadata
        if isinstance(metadatas, Mapping):
            if None in metadatas:
                return metadatas[None]
            if metadatas:
                return next(iter(metadatas.values()))
        return target_db.metadata
    return target_db.metadata


def _is_sqlite_url(url: Optional[str]) -> bool:
    if not url:
        return False
    normalized = url.strip().lower().replace("%%", "%")
    return normalized.startswith("sqlite:")


def _base_configure_kwargs() -> dict[str, Any]:
    """
    Shared Alembic context options for CBT schema fidelity.

    compare_type / compare_server_default improve autogenerate accuracy for
    exam tables (marks FLOAT, enums-as-string, timestamps).
    """
    return {
        "target_metadata": get_metadata(),
        "compare_type": True,
        "compare_server_default": True,
        # --------------------------------------------
        # EXTENSION POINT: include_object / include_name filters for multi-tenant
        # --------------------------------------------
    }


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine. Calls to
    context.execute() emit SQL to the script output. Does not require a DBAPI
    live connection — useful for generating SQL for change-control review
    before applying to a production exam database.
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "sqlalchemy.url is not set — cannot run offline migrations"
        )

    configure_kwargs = _base_configure_kwargs()
    configure_kwargs.update(
        {
            "url": url,
            "literal_binds": True,
            "dialect_opts": {"paramstyle": "named"},
        }
    )
    # SQLite batch mode allows ALTER-style ops on SQLite (default CBT DB)
    if _is_sqlite_url(url):
        configure_kwargs["render_as_batch"] = True

    logger.info(
        "Running offline migrations against %s",
        _mask_database_url(url.replace("%%", "%")),
    )

    context.configure(**configure_kwargs)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode with a live Engine connection.

    Empty autogenerate revisions are discarded so the CBT repo is not polluted
    with no-op migration files during routine `flask db migrate` runs.
    """
    migrate_ext = _require_migrate_extension()

    def process_revision_directives(context_, revision, directives) -> None:  # noqa: ANN001
        """Drop empty autogenerate revisions (no schema delta)."""
        try:
            cmd_opts = getattr(config, "cmd_opts", None)
            autogenerate = bool(getattr(cmd_opts, "autogenerate", False))
        except Exception:  # noqa: BLE001
            autogenerate = False

        if not autogenerate:
            return
        if not directives:
            return

        script = directives[0]
        upgrade_ops = getattr(script, "upgrade_ops", None)
        if upgrade_ops is not None and upgrade_ops.is_empty():
            directives[:] = []
            logger.info("No changes in schema detected.")

    # Copy configure_args so we never permanently mutate the app extension dict
    raw_conf = getattr(migrate_ext, "configure_args", None) or {}
    if isinstance(raw_conf, MutableMapping):
        conf_args: dict[str, Any] = dict(raw_conf)
    else:
        conf_args = dict(raw_conf) if raw_conf else {}

    # Honor an operator-supplied hook; otherwise install empty-revision guard
    conf_args.setdefault("process_revision_directives", process_revision_directives)

    # Merge base compare options without clobbering explicit app overrides
    for key, value in _base_configure_kwargs().items():
        conf_args.setdefault(key, value)

    connectable = get_engine()
    dialect_name = getattr(getattr(connectable, "dialect", None), "name", "") or ""
    if dialect_name == "sqlite":
        conf_args.setdefault("render_as_batch", True)

    logger.info(
        "Running online migrations (dialect=%s)",
        dialect_name or "unknown",
    )

    try:
        with connectable.connect() as connection:
            conf_args["connection"] = connection
            # target_metadata already in conf_args via setdefault
            context.configure(**conf_args)

            with context.begin_transaction():
                context.run_migrations()
    except Exception:
        # Do not attach Engine/URL to the log record — message only
        logger.exception("Online migration failed")
        raise


# ---------------------------------------------------------------------------
# Entrypoint — executed when Alembic loads this module
# ---------------------------------------------------------------------------
try:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
except Exception:
    logger.exception("Alembic environment aborted")
    raise
