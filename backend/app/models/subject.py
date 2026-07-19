"""Subject model — top-level taxonomy for the question bank (Quant, GA, …).

Table: ``subjects``. Counts in ``to_dict`` are optional to avoid N+1 when the
caller already bulk-loads statistics.
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


class Subject(db.Model):
    __tablename__ = "subjects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    code = db.Column(db.String(50), nullable=True, unique=True)
    description = db.Column(db.Text, nullable=True)
    icon = db.Column(db.String(64), nullable=True, default="book")
    color = db.Column(db.String(32), nullable=True, default="#1e40af")
    order_index = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # --------------------------------------------
    # EXTENSION POINT: Add exam_category, language support
    # --------------------------------------------

    chapters = db.relationship(
        "Chapter",
        back_populates="subject",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Chapter.order_index",
    )
    questions = db.relationship("Question", back_populates="subject", lazy="dynamic")

    def to_dict(self, include_counts: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "order_index": self.order_index if self.order_index is not None else 0,
            "is_active": bool(self.is_active),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_counts:
            # dynamic relationships → COUNT queries (same contract as before)
            try:
                data["chapter_count"] = int(self.chapters.count())
            except Exception:  # noqa: BLE001
                data["chapter_count"] = 0
            try:
                data["question_count"] = int(self.questions.count())
            except Exception:  # noqa: BLE001
                data["question_count"] = 0
        return data

    def __repr__(self) -> str:
        return f"<Subject id={self.id} name={self.name!r}>"


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Cached denormalized chapter_count/question_count columns updated by hooks
#   (would need service-layer writers outside this file)
# --------------------------------------------
