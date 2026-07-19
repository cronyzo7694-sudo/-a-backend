"""Exam attempt and per-question answer models.

Lifecycle (status vocabulary is stable)::

    not_started → in_progress → submitted | auto_submitted → evaluated

Security:
    * ``security_flags_json`` append is size-capped (anti log-flood DOS).
    * Answer payloads are JSON-encoded with bounded depth/size expectations
      at the model edge; scoring still lives in ``services.scoring``.

Tables: ``attempts``, ``attempt_answers``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Final, List, Optional, Union

from app.extensions import db

_MAX_SECURITY_FLAGS: Final[int] = 200
_MAX_SECURITY_JSON_CHARS: Final[int] = 100_000
_MAX_SECTION_RESULTS_CHARS: Final[int] = 100_000
_MAX_ANSWER_JSON_CHARS: Final[int] = 10_000


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


def _safe_json_dumps(data: Any, max_chars: int) -> Optional[str]:
    if data is None:
        return None
    try:
        dumped = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(dumped) > max_chars:
        return None
    return dumped


ATTEMPT_STATUSES = (
    "not_started",
    "in_progress",
    "submitted",
    "auto_submitted",
    "evaluated",
)


class Attempt(db.Model):
    __tablename__ = "attempts"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(
        db.Integer,
        db.ForeignKey("exams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status = db.Column(db.String(32), default="in_progress", nullable=False)
    started_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    submitted_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)

    # Snapshot of duration at start
    duration_seconds = db.Column(db.Integer, nullable=False, default=3600)
    time_spent_seconds = db.Column(db.Integer, default=0, nullable=False)

    # Scoring (filled on submit)
    total_questions = db.Column(db.Integer, default=0, nullable=False)
    attempted_count = db.Column(db.Integer, default=0, nullable=False)
    correct_count = db.Column(db.Integer, default=0, nullable=False)
    wrong_count = db.Column(db.Integer, default=0, nullable=False)
    skipped_count = db.Column(db.Integer, default=0, nullable=False)
    score = db.Column(db.Float, default=0.0, nullable=False)
    max_score = db.Column(db.Float, default=0.0, nullable=False)
    percentage = db.Column(db.Float, default=0.0, nullable=False)
    negative_marks_total = db.Column(db.Float, default=0.0, nullable=False)

    # Security
    tab_switch_count = db.Column(db.Integer, default=0, nullable=False)
    security_flags_json = db.Column(db.Text, nullable=True)

    # Section-wise breakdown JSON
    section_results_json = db.Column(db.Text, nullable=True)

    current_section_index = db.Column(db.Integer, default=0, nullable=False)
    current_question_index = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # --------------------------------------------
    # EXTENSION POINT: Add percentile, rank, AI insights blob
    # --------------------------------------------

    exam = db.relationship("Exam", back_populates="attempts")
    user = db.relationship("User", back_populates="attempts")
    answers = db.relationship(
        "AttemptAnswer",
        back_populates="attempt",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def get_security_flags(self) -> List[Any]:
        data = _safe_json_loads(self.security_flags_json, default=[])
        return data if isinstance(data, list) else []

    def add_security_flag(self, flag: dict) -> None:
        """
        Append a proctoring / security event.

        Caps list length so a compromised client cannot grow the row without bound
        during a long mock (memory + row-size DOS).
        """
        if not isinstance(flag, dict):
            return
        # Shallow copy with only JSON-safe primitives preferred
        safe_flag: Dict[str, Any] = {}
        for key, value in list(flag.items())[:32]:
            if not isinstance(key, str) or len(key) > 64:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                if isinstance(value, str) and len(value) > 500:
                    safe_flag[key] = value[:500]
                else:
                    safe_flag[key] = value
            else:
                safe_flag[key] = str(value)[:200]

        flags = self.get_security_flags()
        flags.append(safe_flag)
        if len(flags) > _MAX_SECURITY_FLAGS:
            # Keep newest events — most relevant for force-submit decisions
            flags = flags[-_MAX_SECURITY_FLAGS:]
        dumped = _safe_json_dumps(flags, _MAX_SECURITY_JSON_CHARS)
        if dumped is not None:
            self.security_flags_json = dumped

    def get_section_results(self) -> List[Any]:
        data = _safe_json_loads(self.section_results_json, default=[])
        return data if isinstance(data, list) else []

    def set_section_results(self, results) -> None:
        if not results:
            self.section_results_json = None
            return
        if not isinstance(results, list):
            self.section_results_json = None
            return
        self.section_results_json = _safe_json_dumps(results, _MAX_SECTION_RESULTS_CHARS)

    def is_in_progress(self) -> bool:
        return (self.status or "") == "in_progress"

    def is_terminal(self) -> bool:
        return (self.status or "") in {
            "submitted",
            "auto_submitted",
            "evaluated",
        }

    def to_dict(self, include_answers: bool = False) -> Dict[str, Any]:
        exam_title = None
        user_name = None
        try:
            exam_title = self.exam.title if self.exam else None
        except Exception:  # noqa: BLE001
            exam_title = None
        try:
            user_name = self.user.full_name if self.user else None
        except Exception:  # noqa: BLE001
            user_name = None

        data: Dict[str, Any] = {
            "id": self.id,
            "exam_id": self.exam_id,
            "user_id": self.user_id,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "submitted_at": _iso(self.submitted_at),
            "expires_at": _iso(self.expires_at),
            "duration_seconds": int(self.duration_seconds or 0),
            "time_spent_seconds": int(self.time_spent_seconds or 0),
            "total_questions": int(self.total_questions or 0),
            "attempted_count": int(self.attempted_count or 0),
            "correct_count": int(self.correct_count or 0),
            "wrong_count": int(self.wrong_count or 0),
            "skipped_count": int(self.skipped_count or 0),
            "score": float(self.score) if self.score is not None else 0.0,
            "max_score": float(self.max_score) if self.max_score is not None else 0.0,
            "percentage": float(self.percentage) if self.percentage is not None else 0.0,
            "negative_marks_total": (
                float(self.negative_marks_total)
                if self.negative_marks_total is not None
                else 0.0
            ),
            "tab_switch_count": int(self.tab_switch_count or 0),
            "section_results": self.get_section_results(),
            "current_section_index": int(self.current_section_index or 0),
            "current_question_index": int(self.current_question_index or 0),
            "exam_title": exam_title,
            "user_name": user_name,
        }
        if include_answers:
            try:
                data["answers"] = [a.to_dict() for a in self.answers.all()]
            except Exception:  # noqa: BLE001
                data["answers"] = []
        return data

    def __repr__(self) -> str:
        return (
            f"<Attempt id={self.id} exam_id={self.exam_id} "
            f"user_id={self.user_id} status={self.status!r}>"
        )


class AttemptAnswer(db.Model):
    __tablename__ = "attempt_answers"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(
        db.Integer,
        db.ForeignKey("attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_id = db.Column(
        db.Integer,
        db.ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exam_question_id = db.Column(db.Integer, db.ForeignKey("exam_questions.id"), nullable=True)
    section_id = db.Column(db.Integer, nullable=True)

    # Selected answer — string or JSON string for multi
    selected_answer = db.Column(db.Text, nullable=True)
    is_answered = db.Column(db.Boolean, default=False, nullable=False)
    is_marked_for_review = db.Column(db.Boolean, default=False, nullable=False)
    is_visited = db.Column(db.Boolean, default=False, nullable=False)

    # Evaluation
    is_correct = db.Column(db.Boolean, nullable=True)
    marks_awarded = db.Column(db.Float, default=0.0, nullable=False)
    time_spent_seconds = db.Column(db.Integer, default=0, nullable=False)
    changed_count = db.Column(db.Integer, default=0, nullable=False)

    answered_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    attempt = db.relationship("Attempt", back_populates="answers")
    question = db.relationship("Question", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("attempt_id", "question_id", name="uq_attempt_question"),
    )

    def get_selected_parsed(self) -> Any:
        """
        Parse stored answer.

        JSON arrays/objects decode to Python; plain strings (option keys /
        integers) remain strings. Corrupt JSON falls back to the raw text so
        scoring can still compare string equality.
        """
        if self.selected_answer is None:
            return None
        raw = self.selected_answer
        if not isinstance(raw, str):
            return raw
        stripped = raw.strip()
        if not stripped:
            return None
        # Only attempt JSON parse for structured payloads
        if stripped[0] in "[{":
            parsed = _safe_json_loads(stripped, default=None)
            if parsed is not None:
                return parsed
        return raw

    def set_selected(self, value: Any) -> None:
        """
        Persist a candidate answer and maintain ``is_answered``.

        * None / blank → cleared
        * list/dict → JSON
        * other → string (option key or integer text)
        """
        if value is None:
            self.selected_answer = None
            self.is_answered = False
            return

        if isinstance(value, (list, dict)):
            dumped = _safe_json_dumps(value, _MAX_ANSWER_JSON_CHARS)
            if dumped is None:
                # Oversize / non-serializable — treat as clear rather than partial write
                self.selected_answer = None
                self.is_answered = False
                return
            self.selected_answer = dumped
            # Empty list counts as not answered (cleared multi-select)
            self.is_answered = bool(value)
            return

        text = str(value)
        if len(text) > _MAX_ANSWER_JSON_CHARS:
            text = text[:_MAX_ANSWER_JSON_CHARS]
        self.selected_answer = text
        self.is_answered = bool(text.strip())

    def to_dict(
        self,
        include_question: bool = False,
        include_correct: bool = False,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "attempt_id": self.attempt_id,
            "question_id": self.question_id,
            "exam_question_id": self.exam_question_id,
            "section_id": self.section_id,
            "selected_answer": self.get_selected_parsed(),
            "is_answered": bool(self.is_answered),
            "is_marked_for_review": bool(self.is_marked_for_review),
            "is_visited": bool(self.is_visited),
            "is_correct": self.is_correct,
            "marks_awarded": (
                float(self.marks_awarded) if self.marks_awarded is not None else 0.0
            ),
            "time_spent_seconds": int(self.time_spent_seconds or 0),
            "changed_count": int(self.changed_count or 0),
            "answered_at": _iso(self.answered_at),
        }
        if include_question and self.question is not None:
            try:
                data["question"] = self.question.to_dict(
                    include_answer=include_correct,
                    include_explanation=include_correct,
                )
            except Exception:  # noqa: BLE001
                pass
        return data

    def __repr__(self) -> str:
        return (
            f"<AttemptAnswer id={self.id} attempt_id={self.attempt_id} "
            f"question_id={self.question_id}>"
        )


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Partial index on attempts(user_id, exam_id, status) for resume lookups
# - Immutable score snapshot table post-evaluation for audit / disputes
# - Server-side remaining-time column updated by heartbeat endpoint
# --------------------------------------------
