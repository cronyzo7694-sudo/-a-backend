"""
Knowledge Engine Models - Appearance History & Permanent Intelligence Layer
Additive, never breaks existing schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


class QuestionAppearance(db.Model):
    """
    Appearance History - One canonical question, many appearances
    Example: Same question appears in Pinnacle book page 45 + SSC CGL 2022 Morning + Kiran book
    Never duplicate question, only merge history.
    """
    __tablename__ = "question_appearances"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(
        db.Integer,
        db.ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Metadata of appearance
    exam_name = db.Column(db.String(255), nullable=True, index=True)
    exam_year = db.Column(db.Integer, nullable=True, index=True)
    exam_date = db.Column(db.String(32), nullable=True)
    shift = db.Column(db.String(64), nullable=True)
    session = db.Column(db.String(64), nullable=True)
    organization = db.Column(db.String(255), nullable=True)
    board = db.Column(db.String(128), nullable=True)

    source_book = db.Column(db.String(255), nullable=True, index=True)
    source_type = db.Column(db.String(32), nullable=False, default="book")  # book | pdf | pyq | coaching | ocr | image | typed | ai_generated | word | html | csv | json | other
    page_number = db.Column(db.Integer, nullable=True)
    question_number = db.Column(db.String(32), nullable=True)
    language_detected = db.Column(db.String(16), default="en", nullable=False)

    # Hashes for dedup tracking
    source_hash = db.Column(db.String(64), nullable=True)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    question = db.relationship("Question", backref=db.backref("appearances", cascade="all, delete-orphan", lazy="dynamic"))

    __table_args__ = (
        db.UniqueConstraint(
            "question_id", "exam_name", "exam_year", "shift", "source_book", "page_number",
            name="uq_appearance_question_exam_source"
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "question_id": self.question_id,
            "exam_name": self.exam_name,
            "exam_year": self.exam_year,
            "exam_date": self.exam_date,
            "shift": self.shift,
            "session": self.session,
            "organization": self.organization,
            "board": self.board,
            "source_book": self.source_book,
            "source_type": self.source_type,
            "page_number": self.page_number,
            "question_number": self.question_number,
            "language_detected": self.language_detected,
            "source_hash": self.source_hash,
            "created_at": _iso(self.created_at),
        }


class KnowledgeIngestionJob(db.Model):
    """
    Richer ingestion job for AI Knowledge Engine
    Extends existing ImportJob but with AI-specific stats
    """
    __tablename__ = "knowledge_ingestion_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    source_type = db.Column(db.String(32), nullable=False, default="pdf")  # pdf | image | docx | csv | json | txt | html | md | ocr | typed
    file_name = db.Column(db.String(255), nullable=True)
    file_hash = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="pending")  # pending | processing | completed | failed

    total_blocks_found = db.Column(db.Integer, default=0, nullable=False)
    questions_created = db.Column(db.Integer, default=0, nullable=False)
    duplicates_found = db.Column(db.Integer, default=0, nullable=False)
    needs_review = db.Column(db.Integer, default=0, nullable=False)
    errors_count = db.Column(db.Integer, default=0, nullable=False)

    # Config used
    source_book = db.Column(db.String(255), nullable=True)
    exam_name = db.Column(db.String(255), nullable=True)
    exam_year = db.Column(db.Integer, nullable=True)

    # Results
    errors_json = db.Column(db.Text, nullable=True)
    preview_json = db.Column(db.Text, nullable=True)  # sample of parsed questions
    meta_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        import json
        errors = []
        preview = []
        meta = {}
        try:
            if self.errors_json:
                errors = json.loads(self.errors_json)
        except Exception:
            pass
        try:
            if self.preview_json:
                preview = json.loads(self.preview_json)
        except Exception:
            pass
        try:
            if self.meta_json:
                meta = json.loads(self.meta_json)
        except Exception:
            pass

        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_type": self.source_type,
            "file_name": self.file_name,
            "file_hash": self.file_hash,
            "status": self.status,
            "total_blocks_found": self.total_blocks_found,
            "questions_created": self.questions_created,
            "duplicates_found": self.duplicates_found,
            "needs_review": self.needs_review,
            "errors_count": self.errors_count,
            "source_book": self.source_book,
            "exam_name": self.exam_name,
            "exam_year": self.exam_year,
            "errors": errors[:50] if isinstance(errors, list) else [],
            "preview": preview[:10] if isinstance(preview, list) else [],
            "meta": meta if isinstance(meta, dict) else {},
            "created_at": _iso(self.created_at),
            "completed_at": _iso(self.completed_at),
        }
