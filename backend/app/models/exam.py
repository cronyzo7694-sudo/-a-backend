"""Exam, section, and exam-question assignment models.

CBT exam structure:
    Exam → ExamSection* → ExamQuestion* → Question

``status`` and ``exam_mode`` vocabularies are stable API contracts.
``rules_json`` holds optional advanced rule packs without schema churn.

Tables: ``exams``, ``exam_sections``, ``exam_questions``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Final, List, Optional

from app.extensions import db

_MAX_RULES_JSON_CHARS: Final[int] = 200_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return None


def _safe_json_loads(raw: Optional[str], default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return default


def _safe_json_dumps(data: Any, max_chars: int = _MAX_RULES_JSON_CHARS) -> Optional[str]:
    if data is None:
        return None
    try:
        dumped = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return None
    if len(dumped) > max_chars:
        return None
    return dumped


EXAM_STATUSES = ("draft", "published", "archived")
EXAM_MODES = ("practice", "mock", "sectional", "pyq", "live")


class Exam(db.Model):
    __tablename__ = "exams"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    instructions = db.Column(db.Text, nullable=True)
    exam_mode = db.Column(db.String(32), default="mock", nullable=False)
    status = db.Column(db.String(32), default="draft", nullable=False)

    # Timing (seconds)
    duration_seconds = db.Column(db.Integer, nullable=False, default=3600)
    strict_sections = db.Column(db.Boolean, default=False, nullable=False)

    # Marking
    default_marks = db.Column(db.Float, default=1.0, nullable=False)
    default_negative_marks = db.Column(db.Float, default=0.25, nullable=False)

    # Shuffle
    shuffle_questions = db.Column(db.Boolean, default=False, nullable=False)
    shuffle_options = db.Column(db.Boolean, default=False, nullable=False)

    # Security / UI flags
    require_fullscreen = db.Column(db.Boolean, default=False, nullable=False)
    max_tab_switches = db.Column(db.Integer, default=5, nullable=False)
    show_result_immediately = db.Column(db.Boolean, default=True, nullable=False)

    # Rules JSON blob for advanced config
    rules_json = db.Column(db.Text, nullable=True)

    total_questions = db.Column(db.Integer, default=0, nullable=False)
    total_marks = db.Column(db.Float, default=0.0, nullable=False)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # --------------------------------------------
    # EXTENSION POINT: Add language pack, certificate template, proctoring flags
    # --------------------------------------------

    sections = db.relationship(
        "ExamSection",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ExamSection.order_index",
        lazy="joined",
    )
    exam_questions = db.relationship(
        "ExamQuestion",
        back_populates="exam",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    attempts = db.relationship("Attempt", back_populates="exam", lazy="dynamic")

    def get_rules(self) -> Dict[str, Any]:
        data = _safe_json_loads(self.rules_json, default={})
        return data if isinstance(data, dict) else {}

    def set_rules(self, rules: dict) -> None:
        if not rules:
            self.rules_json = None
            return
        if not isinstance(rules, dict):
            self.rules_json = None
            return
        self.rules_json = _safe_json_dumps(rules)

    def recalculate_totals(self) -> None:
        """
        Recompute denormalized totals from exam_questions.

        Safe when the dynamic relationship is empty or marks are NULL.
        """
        try:
            eqs = list(self.exam_questions.all())
        except Exception:  # noqa: BLE001
            eqs = []
        self.total_questions = len(eqs)
        total = 0.0
        for eq in eqs:
            try:
                total += float(eq.marks or 0.0)
            except (TypeError, ValueError):
                continue
        self.total_marks = total

    def is_published(self) -> bool:
        return (self.status or "") == "published"

    def to_dict(
        self,
        include_sections: bool = True,
        include_questions: bool = False,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "instructions": self.instructions,
            "exam_mode": self.exam_mode,
            "status": self.status,
            "duration_seconds": int(self.duration_seconds or 0),
            "strict_sections": bool(self.strict_sections),
            "default_marks": float(self.default_marks) if self.default_marks is not None else 0.0,
            "default_negative_marks": (
                float(self.default_negative_marks)
                if self.default_negative_marks is not None
                else 0.0
            ),
            "shuffle_questions": bool(self.shuffle_questions),
            "shuffle_options": bool(self.shuffle_options),
            "require_fullscreen": bool(self.require_fullscreen),
            "max_tab_switches": int(self.max_tab_switches or 0),
            "show_result_immediately": bool(self.show_result_immediately),
            "rules": self.get_rules(),
            "total_questions": int(self.total_questions or 0),
            "total_marks": float(self.total_marks) if self.total_marks is not None else 0.0,
            "created_by": self.created_by,
            "starts_at": _iso(self.starts_at),
            "ends_at": _iso(self.ends_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_sections:
            try:
                sections = list(self.sections or [])
            except Exception:  # noqa: BLE001
                sections = []
            data["sections"] = [
                s.to_dict(include_questions=include_questions) for s in sections
            ]
        return data

    def __repr__(self) -> str:
        return f"<Exam id={self.id} status={self.status!r} title={self.title!r}>"


class ExamSection(db.Model):
    __tablename__ = "exam_sections"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(
        db.Integer,
        db.ForeignKey("exams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    order_index = db.Column(db.Integer, default=0, nullable=False)
    duration_seconds = db.Column(db.Integer, nullable=True)  # null = use overall only
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=True)

    exam = db.relationship("Exam", back_populates="sections")
    exam_questions = db.relationship(
        "ExamQuestion",
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="ExamQuestion.order_index",
        lazy="joined",
    )

    def to_dict(self, include_questions: bool = False) -> Dict[str, Any]:
        try:
            eqs = list(self.exam_questions or [])
        except Exception:  # noqa: BLE001
            eqs = []
        data: Dict[str, Any] = {
            "id": self.id,
            "exam_id": self.exam_id,
            "title": self.title,
            "description": self.description,
            "order_index": self.order_index if self.order_index is not None else 0,
            "duration_seconds": self.duration_seconds,
            "subject_id": self.subject_id,
            "question_count": len(eqs),
        }
        if include_questions:
            data["questions"] = [eq.to_dict() for eq in eqs]
        return data

    def __repr__(self) -> str:
        return f"<ExamSection id={self.id} exam_id={self.exam_id} title={self.title!r}>"


class ExamQuestion(db.Model):
    __tablename__ = "exam_questions"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(
        db.Integer,
        db.ForeignKey("exams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_id = db.Column(
        db.Integer,
        db.ForeignKey("exam_sections.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    question_id = db.Column(
        db.Integer,
        db.ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_index = db.Column(db.Integer, default=0, nullable=False)
    marks = db.Column(db.Float, nullable=False, default=1.0)
    negative_marks = db.Column(db.Float, nullable=False, default=0.0)

    exam = db.relationship("Exam", back_populates="exam_questions")
    section = db.relationship("ExamSection", back_populates="exam_questions")
    question = db.relationship("Question", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("exam_id", "question_id", name="uq_exam_question"),
    )

    def to_dict(self, include_answer: bool = False) -> Dict[str, Any]:
        q = None
        try:
            if self.question is not None:
                q = self.question.to_dict(include_answer=include_answer)
        except Exception:  # noqa: BLE001
            q = None
        return {
            "id": self.id,
            "exam_id": self.exam_id,
            "section_id": self.section_id,
            "question_id": self.question_id,
            "order_index": self.order_index if self.order_index is not None else 0,
            "marks": float(self.marks) if self.marks is not None else 0.0,
            "negative_marks": (
                float(self.negative_marks) if self.negative_marks is not None else 0.0
            ),
            "question": q,
        }

    def __repr__(self) -> str:
        return (
            f"<ExamQuestion id={self.id} exam_id={self.exam_id} "
            f"question_id={self.question_id}>"
        )


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Composite indexes (status, exam_mode) for catalog listing at scale
# - Exam window enforcement helpers need route-layer clock injection for tests
# - Partial unique indexes for published slugs once slug column is added
# --------------------------------------------
