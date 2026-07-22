"""Additive schema upgrades for existing SQLite/Postgres DBs.

``db.create_all()`` does not ALTER existing tables. This module adds new
nullable columns / tables required by the enterprise bank & analytics layer
without breaking older rows.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Sequence, Tuple

from sqlalchemy import inspect, text

from app.extensions import db

logger = logging.getLogger("exam_os.services.schema_upgrade")

# (table, column, DDL type fragment) — only ADD COLUMN IF missing
_USER_COLUMNS: Sequence[Tuple[str, str]] = (
    ("google_sub", "VARCHAR(64)"),
    ("auth_provider", "VARCHAR(32) DEFAULT 'password'"),
)

_QUESTION_COLUMNS: Sequence[Tuple[str, str]] = (
    ("bank_id", "INTEGER"),
    ("topic_id", "INTEGER"),
    ("parent_question_id", "INTEGER"),
    ("content_hash", "VARCHAR(64)"),
    ("version", "INTEGER DEFAULT 1"),
    ("question_markdown", "TEXT"),
    ("explanation_markdown", "TEXT"),
    ("status", "VARCHAR(32) DEFAULT 'active'"),
    ("year", "INTEGER"),
    ("shift", "VARCHAR(64)"),
    ("tier", "VARCHAR(64)"),
    ("source", "VARCHAR(255)"),
    ("is_pyq", "BOOLEAN DEFAULT 0"),
    ("is_book", "BOOLEAN DEFAULT 0"),
    ("is_practice", "BOOLEAN DEFAULT 1"),
    ("is_favorite", "BOOLEAN DEFAULT 0"),
    # Knowledge Engine additive columns - permanent intelligence layer
    ("raw_text", "TEXT"),
    ("normalized_question", "TEXT"),
    ("semantic_hash", "VARCHAR(64)"),
    ("source_hash", "VARCHAR(64)"),
    ("qid", "VARCHAR(64)"),
    ("semantic_summary", "TEXT"),
    ("classification_json", "TEXT"),
    ("metadata_json", "TEXT"),
    ("confidence_score", "FLOAT DEFAULT 0.85"),
    ("needs_review", "BOOLEAN DEFAULT 0"),
    ("review_reason", "VARCHAR(255)"),
    ("search_tokens", "TEXT"),
    ("embeddings_text", "TEXT"),
    ("appearance_count", "INTEGER DEFAULT 0"),
    ("language_detected", "VARCHAR(16) DEFAULT 'en'"),
    ("question_family", "VARCHAR(128)"),
    ("pattern", "VARCHAR(128)"),
    ("bloom_taxonomy", "VARCHAR(32)"),
    ("expected_time_seconds", "INTEGER"),
    ("memory_level", "INTEGER DEFAULT 3"),
    ("logic_level", "INTEGER DEFAULT 3"),
    ("calculation_level", "INTEGER DEFAULT 2"),
    # Bilingual support (Hindi). Filled from file if present, else AI-translated
    # on demand and cached here so we never re-translate (saves tokens).
    ("question_text_hi", "TEXT"),
    ("explanation_hi", "TEXT"),
    ("paragraph_text_hi", "TEXT"),
)

_OPTION_COLUMNS: Sequence[Tuple[str, str]] = (
    ("option_text_hi", "TEXT"),
)

_EXAM_COLUMNS: Sequence[Tuple[str, str]] = (
    ("parent_exam_id", "INTEGER"),
)


def _existing_columns(table: str) -> set:
    try:
        bind = db.session.get_bind()
        insp = inspect(bind)
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        logger.exception("inspect columns failed for %s", table)
        return set()


def _table_exists(table: str) -> bool:
    try:
        bind = db.session.get_bind()
        insp = inspect(bind)
        return table in insp.get_table_names()
    except Exception:
        return False


def ensure_additive_schema() -> None:
    """Create new tables via metadata and patch legacy questions table."""
    # New model tables - include knowledge engine tables
    try:
        from app.models import bank as _bank  # noqa: F401
        from app.models import knowledge as _knowledge  # noqa: F401 - Knowledge Engine tables
        db.create_all()
    except Exception:
        logger.exception("create_all during schema_upgrade failed")

    dialect = ""
    try:
        dialect = db.session.get_bind().dialect.name
    except Exception:
        dialect = "sqlite"

    def _add_columns(table: str, columns: Sequence[Tuple[str, str]]) -> None:
        if not _table_exists(table):
            return
        existing = _existing_columns(table)
        for col, col_type in columns:
            if col in existing:
                continue
            ddl = f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
            try:
                db.session.execute(text(ddl))
                db.session.commit()
                logger.info("Added column %s.%s", table, col)
            except Exception:
                db.session.rollback()
                logger.warning("Could not add column %s.%s (may already exist)", table, col)

    _add_columns("users", _USER_COLUMNS)
    _add_columns("questions", _QUESTION_COLUMNS)
    _add_columns("exams", _EXAM_COLUMNS)
    _add_columns("question_options", _OPTION_COLUMNS)

    # Helpful indexes (ignore failures) - including Knowledge Engine indexes
    index_ddls = [
        "CREATE INDEX IF NOT EXISTS ix_questions_content_hash ON questions (content_hash)",
        "CREATE INDEX IF NOT EXISTS ix_questions_bank_id ON questions (bank_id)",
        "CREATE INDEX IF NOT EXISTS ix_questions_status ON questions (status)",
        "CREATE INDEX IF NOT EXISTS ix_questions_year ON questions (year)",
        "CREATE INDEX IF NOT EXISTS ix_questions_semantic_hash ON questions (semantic_hash)",
        "CREATE INDEX IF NOT EXISTS ix_questions_qid ON questions (qid)",
        "CREATE INDEX IF NOT EXISTS ix_questions_needs_review ON questions (needs_review)",
        "CREATE INDEX IF NOT EXISTS ix_questions_subject_id ON questions (subject_id)",
        "CREATE INDEX IF NOT EXISTS ix_questions_chapter_id ON questions (chapter_id)",
        "CREATE INDEX IF NOT EXISTS ix_users_google_sub ON users (google_sub)",
        "CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone)",
        "CREATE INDEX IF NOT EXISTS ix_question_appearances_question_id ON question_appearances (question_id)",
        "CREATE INDEX IF NOT EXISTS ix_question_appearances_exam_name ON question_appearances (exam_name)",
        "CREATE INDEX IF NOT EXISTS ix_question_appearances_source_book ON question_appearances (source_book)",
    ]
    if dialect in ("sqlite", "postgresql"):
        for ddl in index_ddls:
            try:
                db.session.execute(text(ddl))
                db.session.commit()
            except Exception:
                db.session.rollback()
