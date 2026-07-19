"""Shared Flask extensions for Exam OS (CBT platform).

This module is the **single source of truth** for process-wide extension
singletons. Instances are created here *without* an application object and
bound later inside the application factory via ``ext.init_app(app)``.

Why a dedicated module?
    * Avoids circular imports between models, routes, and the app factory.
    * Guarantees one ``SQLAlchemy`` metadata registry for all exam models
      (User, Subject, Chapter, Question, Exam, Attempt, …).
    * Keeps JWT and migration machinery importable from anywhere without
      pulling in the full Flask app at import time.

Public names (stable — imported across the codebase; do not rename):
    db       : flask_sqlalchemy.SQLAlchemy
    jwt      : flask_jwt_extended.JWTManager
    migrate  : flask_migrate.Migrate

Initialization contract (owned by ``app.__init__.create_app``):
    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)

Security posture:
    * No secrets, keys, or connection strings live in this module.
    * Extension objects hold no request state; per-request sessions/tokens
      are resolved by Flask's application/request context.
    * Do not attach mutable global caches here — that becomes a concurrency
      hazard under multi-worker WSGI (gunicorn) during live examinations.

--------------------------------------------
EXTENSION POINT: Additional shared extensions (limiter, mail, cache, …)
must be declared here as uninitialized singletons and wired in create_app.
--------------------------------------------
"""

from __future__ import annotations

from typing import Final

from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

# ---------------------------------------------------------------------------
# SQLAlchemy
# ---------------------------------------------------------------------------
# Default constructor arguments are intentional:
#   * Engine options (pool_pre_ping, etc.) come from app.config
#     (SQLALCHEMY_ENGINE_OPTIONS) so SQLite ↔ Neon switching stays centralized.
#   * session_options are left at framework defaults so existing route/service
#     code that relies on autoflush / expire_on_commit keeps working.
#
# Models import ``db.Model`` / ``db.session`` from this module only.
# ---------------------------------------------------------------------------
db: Final[SQLAlchemy] = SQLAlchemy()

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
# Token creation, verification, and blocklist hooks are registered on the app
# (error loaders in create_app). This object must remain a plain JWTManager()
# so claim loaders and identity handlers attached later continue to bind.
# ---------------------------------------------------------------------------
jwt: Final[JWTManager] = JWTManager()

# ---------------------------------------------------------------------------
# Alembic / Flask-Migrate
# ---------------------------------------------------------------------------
# migrate.init_app(app, db) is performed in the factory. The migrations
# environment (migrations/env.py) reads current_app.extensions["migrate"].
# Do not pass a custom directory here — alembic.ini script_location owns it.
# ---------------------------------------------------------------------------
migrate: Final[Migrate] = Migrate()

# Explicit export surface for ``from app.extensions import *`` safety and
# static analysis. Order is stable for readability only.
__all__ = (
    "db",
    "jwt",
    "migrate",
)


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - flask-limiter / cache / mail singletons would need factory wiring + deps
# - Typed helpers (get_db_session) would require call-site updates
# - session_options={"expire_on_commit": False} can reduce lazy-load surprises
#   after commit in attempt scoring, but changes identity-map semantics app-wide
# --------------------------------------------
