"""Question CRUD routes under ``/api/questions``.

Exam integrity: answer keys are only returned when the caller is admin, or when
``include_answer=true`` is requested by an admin. Students never receive keys
from list/get via role enforcement on the include flag.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Final, List, Optional

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required

from app.extensions import db
from app.models.chapter import Chapter
from app.models.question import DIFFICULTIES, QUESTION_TYPES, Question, QuestionOption
from app.models.subject import Subject
from app.services.question_hash import compute_question_hash, compute_question_hash_from_model
from app.utils.decorators import roles_required
from app.utils.validators import OPTION_KEYS, parse_pagination, require_fields

questions_bp = Blueprint("questions", __name__)
logger = logging.getLogger("exam_os.routes.questions")

_MAX_SEARCH: Final[int] = 200
_MAX_OPTIONS: Final[int] = 10
_MAX_TEXT: Final[int] = 50_000
_MAX_TAGS_STR: Final[int] = 512


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip_str(value: Any, max_len: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:max_len]


def _is_admin() -> bool:
    try:
        return get_jwt().get("role") == "admin"
    except Exception:  # noqa: BLE001
        return False


def _apply_options(question: Question, options: list) -> None:
    """Replace options from payload list of {option_key, option_text, ...}."""
    if not isinstance(options, list):
        return
    question.options.clear()
    db.session.flush()
    for i, opt in enumerate(options[:_MAX_OPTIONS]):
        if not isinstance(opt, dict):
            continue
        key = opt.get("option_key") or (OPTION_KEYS[i] if i < len(OPTION_KEYS) else str(i))
        text = opt.get("option_text", "")
        if text is None:
            text = ""
        text = str(text)[:_MAX_TEXT]
        image_url = _clip_str(opt.get("image_url"), 512)
        if not text and not image_url:
            continue
        order_index = _safe_int(opt.get("order_index"), i)
        if order_index is None:
            order_index = i
        question.options.append(
            QuestionOption(
                option_key=str(key).upper()[:8],
                option_text=text,
                option_html=_clip_str(opt.get("option_html"), _MAX_TEXT),
                image_url=image_url,
                order_index=order_index,
            )
        )


def _encode_correct_answer(correct: Any) -> str:
    if isinstance(correct, list):
        return json.dumps([str(x) for x in correct])
    return str(correct)


def _encode_tags(tags: Any) -> Optional[str]:
    if tags is None:
        return None
    if isinstance(tags, list):
        parts = [str(t).strip() for t in tags if str(t).strip()]
        return ",".join(parts)[:_MAX_TAGS_STR]
    return str(tags)[:_MAX_TAGS_STR]


@questions_bp.get("")
@jwt_required()
def list_questions():
    page, per_page = parse_pagination(request.args)
    q = Question.query

    sid = request.args.get("subject_id")
    if sid not in (None, ""):
        parsed = _safe_int(sid)
        if parsed is None:
            return jsonify({"error": "Invalid subject_id"}), 400
        q = q.filter_by(subject_id=parsed)

    cid = request.args.get("chapter_id")
    if cid not in (None, ""):
        parsed = _safe_int(cid)
        if parsed is None:
            return jsonify({"error": "Invalid chapter_id"}), 400
        q = q.filter_by(chapter_id=parsed)

    qtype = request.args.get("question_type")
    if qtype:
        if qtype not in QUESTION_TYPES:
            return jsonify({"error": "Invalid question_type"}), 400
        q = q.filter_by(question_type=qtype)

    difficulty = request.args.get("difficulty")
    if difficulty:
        if difficulty not in DIFFICULTIES:
            return jsonify({"error": "Invalid difficulty"}), 400
        q = q.filter_by(difficulty=difficulty)

    if request.args.get("active_only", "true").lower() == "true":
        q = q.filter_by(is_active=True)

    search = (request.args.get("search") or "").strip()[:_MAX_SEARCH]
    if search:
        q = q.filter(
            Question.question_text.ilike(f"%{_escape_like(search)}%", escape="\\")
        )

    total = q.count()
    items = (
        q.order_by(Question.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # Answer keys only for admins — students cannot exfiltrate bank via flag
    want_answer = request.args.get("include_answer", "false").lower() == "true"
    include_answer = want_answer and _is_admin()

    return jsonify({
        "items": [
            i.to_dict(include_answer=include_answer, include_explanation=include_answer)
            for i in items
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
    })


@questions_bp.get("/types")
@jwt_required()
def question_types():
    return jsonify({
        "question_types": list(QUESTION_TYPES),
        "difficulties": list(DIFFICULTIES),
    })


@questions_bp.get("/<int:question_id>")
@jwt_required()
def get_question(question_id):
    q = Question.query.get_or_404(question_id)
    want = request.args.get("include_answer", "true").lower() == "true"
    # Default true preserved for admin UI; students never get keys
    include = want and _is_admin()
    return jsonify(q.to_dict(include_answer=include, include_explanation=include))


@questions_bp.post("")
@roles_required("admin")
def create_question():
    data = _json_body()
    err = require_fields(data, ["question_text", "question_type", "correct_answer"])
    if err:
        return jsonify({"error": err}), 400

    qtype = data["question_type"]
    if qtype not in QUESTION_TYPES:
        return jsonify({
            "error": f"Invalid question_type. Allowed: {', '.join(QUESTION_TYPES)}"
        }), 400

    difficulty = data.get("difficulty", "medium")
    if difficulty not in DIFFICULTIES:
        difficulty = "medium"

    subject_id = _safe_int(data.get("subject_id")) if data.get("subject_id") not in (None, "") else None
    chapter_id = _safe_int(data.get("chapter_id")) if data.get("chapter_id") not in (None, "") else None
    if data.get("subject_id") not in (None, "") and subject_id is None:
        return jsonify({"error": "Invalid subject_id"}), 400
    if data.get("chapter_id") not in (None, "") and chapter_id is None:
        return jsonify({"error": "Invalid chapter_id"}), 400
    if subject_id is not None and not Subject.query.get(subject_id):
        return jsonify({"error": "Subject not found"}), 404
    if chapter_id is not None and not Chapter.query.get(chapter_id):
        return jsonify({"error": "Chapter not found"}), 404

    qtext = _clip_str(data["question_text"], _MAX_TEXT)
    if not qtext or not qtext.strip():
        return jsonify({"error": "question_text is required"}), 400

    try:
        created_by = int(get_jwt_identity())
    except (TypeError, ValueError):
        created_by = None

    time_seconds = None
    if data.get("time_seconds") not in (None, ""):
        time_seconds = _safe_int(data.get("time_seconds"))
        if time_seconds is not None and time_seconds < 0:
            time_seconds = None

    q = Question(
        subject_id=subject_id,
        chapter_id=chapter_id,
        question_type=qtype,
        difficulty=difficulty,
        question_text=qtext.strip(),
        question_html=_clip_str(data.get("question_html"), _MAX_TEXT),
        explanation=_clip_str(data.get("explanation"), _MAX_TEXT),
        explanation_html=_clip_str(data.get("explanation_html"), _MAX_TEXT),
        paragraph_text=_clip_str(data.get("paragraph_text"), _MAX_TEXT),
        paragraph_html=_clip_str(data.get("paragraph_html"), _MAX_TEXT),
        image_url=_clip_str(data.get("image_url"), 512),
        marks=_safe_float(data.get("marks", 1.0), 1.0),
        negative_marks=_safe_float(data.get("negative_marks", 0.0), 0.0),
        time_seconds=time_seconds,
        correct_answer=_encode_correct_answer(data["correct_answer"]),
        tags=_encode_tags(data.get("tags")),
        language=_clip_str(data.get("language") or "en", 32) or "en",
        is_active=bool(data.get("is_active", True)),
        created_by=created_by,
    )
    if data.get("media") is not None:
        if isinstance(data.get("media"), dict):
            q.set_media(data["media"])
        elif data.get("media") in (None, {}, []):
            q.set_media({})

    db.session.add(q)
    db.session.flush()

    if qtype != "integer":
        _apply_options(q, data.get("options", []))

    # Bank / metadata (additive)
    if data.get("bank_id") not in (None, ""):
        q.bank_id = _safe_int(data.get("bank_id"))
    if data.get("topic_id") not in (None, ""):
        q.topic_id = _safe_int(data.get("topic_id"))
    for meta in ("year",):
        if data.get(meta) not in (None, ""):
            setattr(q, meta, _safe_int(data.get(meta)))
    for meta in ("shift", "tier", "source", "status", "question_markdown", "explanation_markdown"):
        if meta in data and data[meta] is not None:
            setattr(q, meta, str(data[meta])[:255] if meta in ("shift", "tier", "source", "status") else data[meta])
    for flag in ("is_pyq", "is_book", "is_practice", "is_favorite"):
        if flag in data:
            setattr(q, flag, bool(data[flag]))

    try:
        q.content_hash = compute_question_hash_from_model(q)
    except Exception:
        logger.exception("hash compute failed on create")

    # Duplicate detection
    if q.content_hash:
        dup = Question.query.filter(
            Question.content_hash == q.content_hash,
            Question.id != (q.id or 0),
        ).first()
        if dup:
            db.session.rollback()
            return jsonify({
                "error": "Duplicate question detected",
                "duplicate_id": dup.id,
                "content_hash": q.content_hash,
            }), 409

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_question failed")
        return jsonify({"error": "Could not create question"}), 500

    return jsonify({
        "message": "Question created",
        "item": q.to_dict(include_answer=True, include_explanation=True),
    }), 201


@questions_bp.put("/<int:question_id>")
@roles_required("admin")
def update_question(question_id):
    q = Question.query.get_or_404(question_id)
    data = _json_body()

    for field in (
        "question_text", "question_html", "explanation", "explanation_html",
        "paragraph_text", "paragraph_html", "image_url", "language",
    ):
        if field in data:
            max_len = 512 if field == "image_url" else (_MAX_TEXT if field != "language" else 32)
            val = data[field]
            if val is None:
                setattr(q, field, None)
            else:
                setattr(q, field, str(val)[:max_len])

    if "question_type" in data:
        if data["question_type"] not in QUESTION_TYPES:
            return jsonify({"error": "Invalid question_type"}), 400
        q.question_type = data["question_type"]
    if "difficulty" in data and data["difficulty"] in DIFFICULTIES:
        q.difficulty = data["difficulty"]
    if "subject_id" in data:
        if data["subject_id"] in (None, ""):
            q.subject_id = None
        else:
            sid = _safe_int(data["subject_id"])
            if sid is None:
                return jsonify({"error": "Invalid subject_id"}), 400
            q.subject_id = sid
    if "chapter_id" in data:
        if data["chapter_id"] in (None, ""):
            q.chapter_id = None
        else:
            cid = _safe_int(data["chapter_id"])
            if cid is None:
                return jsonify({"error": "Invalid chapter_id"}), 400
            q.chapter_id = cid
    if "marks" in data:
        q.marks = _safe_float(data["marks"], q.marks or 1.0)
    if "negative_marks" in data:
        q.negative_marks = _safe_float(data["negative_marks"], q.negative_marks or 0.0)
    if "time_seconds" in data:
        q.time_seconds = (
            _safe_int(data["time_seconds"])
            if data["time_seconds"] not in (None, "")
            else None
        )
    if "is_active" in data:
        q.is_active = bool(data["is_active"])
    if "tags" in data:
        q.tags = _encode_tags(data["tags"])
    if "correct_answer" in data:
        q.correct_answer = _encode_correct_answer(data["correct_answer"])
    if "media" in data:
        if isinstance(data["media"], dict):
            q.set_media(data["media"])
        elif not data["media"]:
            q.set_media({})
    if "options" in data:
        _apply_options(q, data["options"])

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("update_question id=%s failed", question_id)
        return jsonify({"error": "Could not update question"}), 500

    return jsonify({
        "message": "Question updated",
        "item": q.to_dict(include_answer=True, include_explanation=True),
    })


@questions_bp.delete("/<int:question_id>")
@roles_required("admin")
def delete_question(question_id):
    q = Question.query.get_or_404(question_id)
    db.session.delete(q)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_question id=%s failed", question_id)
        return jsonify({"error": "Could not delete question"}), 500
    return jsonify({"message": "Question deleted"})


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Soft-delete + exam reference checks before hard delete
# - Full-text search via Postgres tsvector
# --------------------------------------------
