"""Chapter model — second-level taxonomy under a Subject.

Table: ``chapters``. Unique (subject_id, name) prevents duplicate chapters in
the same subject (import / admin double-submit safety at DB layer).
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
    except Exception:  # noqa: BLE001
        return None


class Chapter(db.Model):
    __tablename__ = "chapters"

    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(
        db.Integer,
        db.ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    order_index = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # --------------------------------------------
    # EXTENSION POINT: Add topic hierarchy under chapters
    # --------------------------------------------

    subject = db.relationship("Subject", back_populates="chapters")
    questions = db.relationship("Question", back_populates="chapter", lazy="dynamic")

    __table_args__ = (
        db.UniqueConstraint("subject_id", "name", name="uq_chapter_subject_name"),
    )

    def to_dict(self, include_counts: bool = True) -> Dict[str, Any]:
        subject_name = None
        try:
            subject_name = self.subject.name if self.subject else None
        except Exception:  # noqa: BLE001 — detached instance safety
            subject_name = None

        data: Dict[str, Any] = {
            "id": self.id,
            "subject_id": self.subject_id,
            "name": self.name,
            "description": self.description,
            "order_index": self.order_index if self.order_index is not None else 0,
            "is_active": bool(self.is_active),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "subject_name": subject_name,
        }
        if include_counts:
            try:
                data["question_count"] = int(self.questions.count())
            except Exception:  # noqa: BLE001
                data["question_count"] = 0
        return data

    def __repr__(self) -> str:
        return f"<Chapter id={self.id} subject_id={self.subject_id} name={self.name!r}>"


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Topic child table + ordered tree for SSC micro-topics
# --------------------------------------------
