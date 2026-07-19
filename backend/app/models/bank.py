"""Question Bank domain — reusable banks, tags, topics, import jobs.

Tables are additive. Existing ``questions`` / ``exam_questions`` keep working.
A question may belong to a bank and be mapped into unlimited exams via
``ExamQuestion`` (existing mapping table).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


class QuestionBank(db.Model):
    __tablename__ = "question_banks"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    code = db.Column(db.String(64), nullable=True, unique=True)
    description = db.Column(db.Text, nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    questions = db.relationship("Question", back_populates="bank", lazy="dynamic")

    def to_dict(self, include_counts: bool = True) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "description": self.description,
            "owner_id": self.owner_id,
            "is_active": bool(self.is_active),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_counts:
            try:
                data["question_count"] = int(self.questions.count())
            except Exception:
                data["question_count"] = 0
        return data


class Topic(db.Model):
    """Topic / sub-topic under a chapter (optional hierarchy via parent_id)."""

    __tablename__ = "topics"

    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(
        db.Integer, db.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    parent_id = db.Column(db.Integer, db.ForeignKey("topics.id", ondelete="SET NULL"), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    order_index = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    parent = db.relationship("Topic", remote_side=[id], backref="children")

    __table_args__ = (
        db.UniqueConstraint("chapter_id", "name", "parent_id", name="uq_topic_chapter_name_parent"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "chapter_id": self.chapter_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "order_index": self.order_index or 0,
            "is_active": bool(self.is_active),
        }


class Tag(db.Model):
    __tablename__ = "tags"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, unique=True, index=True)
    slug = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "slug": self.slug}


class QuestionTag(db.Model):
    __tablename__ = "question_tags"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(
        db.Integer, db.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id = db.Column(
        db.Integer, db.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        db.UniqueConstraint("question_id", "tag_id", name="uq_question_tag"),
    )


class ImportJob(db.Model):
    """Import history / logs for CSV/JSON (and future Excel/OCR)."""

    __tablename__ = "import_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    source_type = db.Column(db.String(32), nullable=False, default="json")  # csv|json|excel|pdf
    status = db.Column(db.String(32), nullable=False, default="pending")
    file_name = db.Column(db.String(255), nullable=True)
    total_rows = db.Column(db.Integer, default=0, nullable=False)
    success_count = db.Column(db.Integer, default=0, nullable=False)
    error_count = db.Column(db.Integer, default=0, nullable=False)
    duplicate_count = db.Column(db.Integer, default=0, nullable=False)
    bank_id = db.Column(db.Integer, db.ForeignKey("question_banks.id"), nullable=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey("chapters.id"), nullable=True)
    errors_json = db.Column(db.Text, nullable=True)
    meta_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        import json

        errors = []
        meta = {}
        try:
            if self.errors_json:
                errors = json.loads(self.errors_json)
        except Exception:
            errors = []
        try:
            if self.meta_json:
                meta = json.loads(self.meta_json)
        except Exception:
            meta = {}
        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_type": self.source_type,
            "status": self.status,
            "file_name": self.file_name,
            "total_rows": self.total_rows,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "duplicate_count": self.duplicate_count,
            "bank_id": self.bank_id,
            "exam_id": self.exam_id,
            "subject_id": self.subject_id,
            "chapter_id": self.chapter_id,
            "errors": errors if isinstance(errors, list) else [],
            "meta": meta if isinstance(meta, dict) else {},
            "created_at": _iso(self.created_at),
            "completed_at": _iso(self.completed_at),
        }


class AuditLog(db.Model):
    """Lightweight security / admin audit trail."""

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    resource_type = db.Column(db.String(64), nullable=True)
    resource_id = db.Column(db.String(64), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    detail_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    def to_dict(self) -> Dict[str, Any]:
        import json

        detail = {}
        try:
            if self.detail_json:
                detail = json.loads(self.detail_json)
        except Exception:
            detail = {}
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "ip_address": self.ip_address,
            "created_at": _iso(self.created_at),
            "detail": detail,
        }
