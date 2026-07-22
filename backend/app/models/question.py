"""Question and option models for the CBT question bank.

Supported types (stable vocabulary — do not rename values)::

    single_choice | multiple_choice | integer | paragraph | image | math

Security:
    * ``correct_answer`` is omitted from ``to_dict`` unless ``include_answer``
      is explicitly True (exam player must never receive keys mid-test).
    * JSON helpers never raise on corrupt rows (admin import edge cases).
    * Media / tags parsing is defensive against oversized or malformed blobs.

Tables: ``questions``, ``question_options``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Final, List, Optional, Sequence, Union

from app.extensions import db

# Bound JSON payload size when serializing media (anti memory blow-up)
_MAX_MEDIA_JSON_CHARS: Final[int] = 100_000
_MAX_TAGS: Final[int] = 50
_MAX_TAG_LEN: Final[int] = 64


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


def _safe_json_dumps(data: Any) -> Optional[str]:
    if data is None:
        return None
    try:
        dumped = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return None
    if len(dumped) > _MAX_MEDIA_JSON_CHARS:
        # Refuse to persist pathological media blobs via the model helper
        return None
    return dumped


# Supported question types — do not rename
QUESTION_TYPES = (
    "single_choice",
    "multiple_choice",
    "integer",
    "paragraph",
    "image",
    "math",
)

DIFFICULTIES = ("easy", "medium", "hard")


class Question(db.Model):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(
        db.Integer,
        db.ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    chapter_id = db.Column(
        db.Integer,
        db.ForeignKey("chapters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Additive bank fields (nullable — backward compatible)
    bank_id = db.Column(
        db.Integer,
        db.ForeignKey("question_banks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    topic_id = db.Column(
        db.Integer,
        db.ForeignKey("topics.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    parent_question_id = db.Column(
        db.Integer,
        db.ForeignKey("questions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    content_hash = db.Column(db.String(64), nullable=True, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)

    question_type = db.Column(db.String(32), nullable=False, default="single_choice")
    difficulty = db.Column(db.String(16), nullable=False, default="medium")

    # Text / HTML / Markdown (MathJax-friendly)
    question_text = db.Column(db.Text, nullable=False)
    question_html = db.Column(db.Text, nullable=True)
    question_markdown = db.Column(db.Text, nullable=True)
    question_text_hi = db.Column(db.Text, nullable=True)  # Hindi (file or AI-cached)
    explanation = db.Column(db.Text, nullable=True)
    explanation_html = db.Column(db.Text, nullable=True)
    explanation_hi = db.Column(db.Text, nullable=True)  # Hindi explanation
    paragraph_text_hi = db.Column(db.Text, nullable=True)  # Hindi paragraph stem
    explanation_markdown = db.Column(db.Text, nullable=True)

    # Paragraph stem (for paragraph-based questions)
    paragraph_text = db.Column(db.Text, nullable=True)
    paragraph_html = db.Column(db.Text, nullable=True)

    # Media
    image_url = db.Column(db.String(512), nullable=True)
    media_json = db.Column(db.Text, nullable=True)  # extra media blob

    # Scoring defaults
    marks = db.Column(db.Float, nullable=False, default=1.0)
    negative_marks = db.Column(db.Float, nullable=False, default=0.0)
    time_seconds = db.Column(db.Integer, nullable=True)

    # Correct answer storage
    # single_choice / image / math / paragraph → option key e.g. "A"
    # multiple_choice → JSON list e.g. ["A","C"]
    # integer → string number e.g. "42"
    correct_answer = db.Column(db.Text, nullable=False)

    tags = db.Column(db.String(512), nullable=True)  # comma-separated (legacy + search)
    language = db.Column(db.String(32), default="en", nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    # status: active | hidden | archived (is_active kept for compatibility)
    status = db.Column(db.String(32), nullable=False, default="active", index=True)
    # Metadata for PYQ / books / practice banks
    year = db.Column(db.Integer, nullable=True, index=True)
    shift = db.Column(db.String(64), nullable=True)
    tier = db.Column(db.String(64), nullable=True)
    source = db.Column(db.String(255), nullable=True)
    is_pyq = db.Column(db.Boolean, default=False, nullable=False)
    is_book = db.Column(db.Boolean, default=False, nullable=False)
    is_practice = db.Column(db.Boolean, default=True, nullable=False)
    is_favorite = db.Column(db.Boolean, default=False, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # --------------------------------------------
    # EXTENSION POINT: bilingual packs, bloom taxonomy
    # --------------------------------------------

    subject = db.relationship("Subject", back_populates="questions")
    chapter = db.relationship("Chapter", back_populates="questions")
    bank = db.relationship("QuestionBank", back_populates="questions")
    options = db.relationship(
        "QuestionOption",
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="QuestionOption.order_index",
        lazy="joined",
    )

    def get_media(self) -> Dict[str, Any]:
        """Parse media_json → dict. Corrupt data yields empty dict."""
        data = _safe_json_loads(self.media_json, default={})
        return data if isinstance(data, dict) else {}

    def set_media(self, data: dict) -> None:
        """Serialize media dict. Non-dicts / empty clear the column."""
        if not data:
            self.media_json = None
            return
        if not isinstance(data, dict):
            self.media_json = None
            return
        dumped = _safe_json_dumps(data)
        # If serialization failed or oversize, leave prior value only when dump fails hard
        self.media_json = dumped

    def get_correct_answer_parsed(self) -> Union[str, List[Any], None]:
        """
        Return correct answer in the shape expected by the scoring engine.

        * multiple_choice → list of keys
        * integer / others → raw string (or original text)
        """
        raw = self.correct_answer
        if raw is None:
            return None

        qtype = (self.question_type or "single_choice").lower()

        if qtype == "multiple_choice":
            parsed = _safe_json_loads(raw, default=None)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
            if isinstance(parsed, str) and parsed.strip():
                return [parsed.strip()]
            # Comma-separated fallback from CSV import
            if isinstance(raw, str) and "," in raw:
                return [p.strip() for p in raw.split(",") if p.strip()]
            return [str(raw)] if str(raw).strip() else []

        if qtype == "integer":
            return raw if isinstance(raw, str) else str(raw)

        return raw if isinstance(raw, str) else str(raw)

    def _parse_tags(self) -> List[str]:
        if not self.tags:
            return []
        out: List[str] = []
        seen = set()
        for part in str(self.tags).split(","):
            tag = part.strip()
            if not tag or len(tag) > _MAX_TAG_LEN:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(tag)
            if len(out) >= _MAX_TAGS:
                break
        return out

    def to_dict(
        self,
        include_answer: bool = False,
        include_explanation: bool = False,
    ) -> Dict[str, Any]:
        subject_name = None
        chapter_name = None
        try:
            subject_name = self.subject.name if self.subject else None
        except Exception:  # noqa: BLE001
            subject_name = None
        try:
            chapter_name = self.chapter.name if self.chapter else None
        except Exception:  # noqa: BLE001
            chapter_name = None

        try:
            options_payload = [o.to_dict() for o in (self.options or [])]
        except Exception:  # noqa: BLE001
            options_payload = []

        data: Dict[str, Any] = {
            "id": self.id,
            "subject_id": self.subject_id,
            "chapter_id": self.chapter_id,
            "bank_id": getattr(self, "bank_id", None),
            "topic_id": getattr(self, "topic_id", None),
            "parent_question_id": getattr(self, "parent_question_id", None),
            "content_hash": getattr(self, "content_hash", None),
            "version": int(getattr(self, "version", 1) or 1),
            "subject_name": subject_name,
            "chapter_name": chapter_name,
            "question_type": self.question_type,
            "difficulty": self.difficulty,
            "question_text": self.question_text,
            "question_html": self.question_html,
            "question_markdown": getattr(self, "question_markdown", None),
            "question_text_hi": getattr(self, "question_text_hi", None),
            "paragraph_text": self.paragraph_text,
            "paragraph_html": self.paragraph_html,
            "paragraph_text_hi": getattr(self, "paragraph_text_hi", None),
            "image_url": self.image_url,
            "media": self.get_media(),
            "marks": float(self.marks) if self.marks is not None else 0.0,
            "negative_marks": float(self.negative_marks) if self.negative_marks is not None else 0.0,
            "time_seconds": self.time_seconds,
            "tags": self._parse_tags(),
            "language": self.language or "en",
            "is_active": bool(self.is_active),
            "status": getattr(self, "status", None) or ("active" if self.is_active else "hidden"),
            "year": getattr(self, "year", None),
            "shift": getattr(self, "shift", None),
            "tier": getattr(self, "tier", None),
            "source": getattr(self, "source", None),
            "is_pyq": bool(getattr(self, "is_pyq", False)),
            "is_book": bool(getattr(self, "is_book", False)),
            "is_practice": bool(getattr(self, "is_practice", True)),
            "is_favorite": bool(getattr(self, "is_favorite", False)),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "options": options_payload,
        }
        # Answer key / explanation are opt-in — critical for exam integrity
        if include_answer:
            data["correct_answer"] = self.get_correct_answer_parsed()
        if include_explanation:
            data["explanation"] = self.explanation
            data["explanation_html"] = self.explanation_html
            data["explanation_hi"] = getattr(self, "explanation_hi", None)
        return data

    def __repr__(self) -> str:
        return f"<Question id={self.id} type={self.question_type!r}>"


class QuestionOption(db.Model):
    __tablename__ = "question_options"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(
        db.Integer,
        db.ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    option_key = db.Column(db.String(8), nullable=False)  # A, B, C, D, E...
    option_text = db.Column(db.Text, nullable=False)
    option_html = db.Column(db.Text, nullable=True)
    option_text_hi = db.Column(db.Text, nullable=True)  # Hindi (file or AI-cached)
    image_url = db.Column(db.String(512), nullable=True)
    order_index = db.Column(db.Integer, default=0, nullable=False)

    question = db.relationship("Question", back_populates="options")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "option_key": self.option_key,
            "option_text": self.option_text,
            "option_html": self.option_html,
            "option_text_hi": self.option_text_hi,
            "image_url": self.image_url,
            "order_index": self.order_index if self.order_index is not None else 0,
        }

    def __repr__(self) -> str:
        return f"<QuestionOption id={self.id} key={self.option_key!r}>"


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Separate answer_key table with encryption at rest for high-stakes exams
# - Full-text index on question_text (Postgres tsvector) for bank search
# - Versioning / audit trail for edited PYQ items
# --------------------------------------------
