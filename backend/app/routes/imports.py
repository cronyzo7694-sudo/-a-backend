"""Professional Import Engine — CSV / JSON (+ future Excel / OCR).

Stable routes under ``/api/imports``:
    POST /questions/json
    POST /questions/csv
    GET  /questions/template
    POST /questions/preview   (additive)
    GET  /jobs                (additive history)
    GET  /jobs/<id>
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Final, List, Optional, Tuple

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.bank import ImportJob, QuestionBank, Tag, QuestionTag, Topic
from app.models.chapter import Chapter
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.question import DIFFICULTIES, QUESTION_TYPES, Question, QuestionOption
from app.models.subject import Subject
from app.services.question_hash import compute_question_hash
from app.utils.decorators import roles_required
from app.utils.validators import OPTION_KEYS

# Knowledge Engine - Internal Brain (additive, never breaks existing)
try:
    from app.services.knowledge_engine.pipeline import knowledge_engine
    from app.models.knowledge import QuestionAppearance, KnowledgeIngestionJob
    KNOWLEDGE_ENGINE_AVAILABLE = True
except Exception as e:
    # Fallback if engine not fully loaded (e.g., during migrations)
    knowledge_engine = None
    QuestionAppearance = None
    KnowledgeIngestionJob = None
    KNOWLEDGE_ENGINE_AVAILABLE = False
    # Will log at runtime

imports_bp = Blueprint("imports", __name__)
logger = logging.getLogger("exam_os.routes.imports")

_MAX_JSON_ROWS: Final[int] = 1000
_MAX_CSV_ROWS: Final[int] = 2000
_MAX_CSV_BYTES: Final[int] = 5 * 1024 * 1024
_MAX_TEXT: Final[int] = 50_000
_MAX_OPTIONS: Final[int] = 10
_MAX_ERRORS_STORED: Final[int] = 200

# Knowledge Engine limits - supports thousands per import
_MAX_AI_TEXT_BYTES: Final[int] = 10 * 1024 * 1024  # 10MB raw text
_MAX_AI_FILE_BYTES: Final[int] = 20 * 1024 * 1024  # 20MB file
_MAX_AI_QUESTIONS_PER_BATCH: Final[int] = 5000


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _identity_int() -> Optional[int]:
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clip(value: Any, max_len: int) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:max_len]


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (s or "tag")[:64]


def _detect_difficulty(text: str, explicit: Optional[str] = None) -> str:
    if explicit and str(explicit).strip().lower() in DIFFICULTIES:
        return str(explicit).strip().lower()
    t = (text or "").lower()
    if any(w in t for w in ("assert", "prove", "complex", "paragraph", "passage")):
        return "hard"
    if any(w in t for w in ("find", "calculate", "which of the following")):
        return "medium"
    return "easy"


def _ensure_tags(tag_names: List[str]) -> List[Tag]:
    out: List[Tag] = []
    for raw in tag_names[:30]:
        name = str(raw).strip()[:64]
        if not name:
            continue
        slug = _slugify(name)
        tag = Tag.query.filter((Tag.slug == slug) | (Tag.name == name)).first()
        if not tag:
            tag = Tag(name=name, slug=slug)
            db.session.add(tag)
            db.session.flush()
        out.append(tag)
    return out


def _resolve_subject_chapter(
    row: Dict[str, Any],
    default_subject_id=None,
    default_chapter_id=None,
    auto_create: bool = False,
) -> Tuple[Optional[int], Optional[int]]:
    subject_id = default_subject_id
    chapter_id = default_chapter_id
    if row.get("subject_id") not in (None, ""):
        parsed = _safe_int(row.get("subject_id"))
        if parsed is not None:
            subject_id = parsed
    elif row.get("subject"):
        name = str(row["subject"]).strip()[:200]
        if name:
            s = Subject.query.filter(Subject.name.ilike(name)).first()
            if not s and auto_create:
                s = Subject(name=name, is_active=True)
                db.session.add(s)
                db.session.flush()
            if s:
                subject_id = s.id
    if row.get("chapter_id") not in (None, ""):
        parsed = _safe_int(row.get("chapter_id"))
        if parsed is not None:
            chapter_id = parsed
    elif row.get("chapter") and subject_id:
        cname = str(row["chapter"]).strip()[:200]
        if cname:
            c = Chapter.query.filter(
                Chapter.subject_id == subject_id,
                Chapter.name.ilike(cname),
            ).first()
            if not c and auto_create:
                c = Chapter(subject_id=subject_id, name=cname, is_active=True)
                db.session.add(c)
                db.session.flush()
            if c:
                chapter_id = c.id
    return subject_id, chapter_id


def _resolve_topic(row: Dict[str, Any], chapter_id: Optional[int], auto_create: bool = False) -> Optional[int]:
    if row.get("topic_id") not in (None, ""):
        return _safe_int(row.get("topic_id"))
    name = row.get("topic") or row.get("sub_topic")
    if not name:
        return None
    name = str(name).strip()[:200]
    parent_id = None
    if row.get("sub_topic") and row.get("topic"):
        parent = Topic.query.filter(
            Topic.chapter_id == chapter_id,
            Topic.name.ilike(str(row.get("topic")).strip()[:200]),
            Topic.parent_id.is_(None),
        ).first()
        if not parent and auto_create and chapter_id:
            parent = Topic(chapter_id=chapter_id, name=str(row.get("topic")).strip()[:200])
            db.session.add(parent)
            db.session.flush()
        parent_id = parent.id if parent else None
        name = str(row.get("sub_topic")).strip()[:200]
    q = Topic.query.filter(Topic.name.ilike(name))
    if chapter_id:
        q = q.filter_by(chapter_id=chapter_id)
    if parent_id:
        q = q.filter_by(parent_id=parent_id)
    topic = q.first()
    if not topic and auto_create:
        topic = Topic(chapter_id=chapter_id, parent_id=parent_id, name=name)
        db.session.add(topic)
        db.session.flush()
    return topic.id if topic else None


def _resolve_exam_id(
    row: Optional[Dict[str, Any]] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """
    Resolve which exam to attach a question to.

    Accepts (in order of precedence on the row, then defaults):
      exam_id (int)
      exam / exam_name / exam_title (string — matched case-insensitively on Exam.title)

    Questions without any exam field stay in the bank only (reusable).
    """
    row = row or {}
    defaults = defaults or {}

    for source in (row, defaults):
        if source.get("exam_id") not in (None, ""):
            eid = _safe_int(source.get("exam_id"))
            if eid is not None and Exam.query.get(eid):
                return eid

    for source in (row, defaults):
        for key in ("exam", "exam_name", "exam_title", "exam_title_name"):
            raw = source.get(key)
            if raw in (None, ""):
                continue
            # numeric string treated as id
            as_id = _safe_int(raw)
            if as_id is not None and str(raw).strip().isdigit():
                if Exam.query.get(as_id):
                    return as_id
            name = str(raw).strip()[:255]
            if not name:
                continue
            # exact title match first, then case-insensitive equality
            exam = Exam.query.filter(Exam.title == name).first()
            if not exam:
                exam = Exam.query.filter(Exam.title.ilike(name)).first()
            if not exam:
                # partial contains match only if unique
                matches = Exam.query.filter(Exam.title.ilike(f"%{name}%")).limit(3).all()
                if len(matches) == 1:
                    exam = matches[0]
            if exam:
                return exam.id
    return None


def _resolve_section_id(
    exam_id: int,
    row: Optional[Dict[str, Any]] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Resolve section_id or section / section_name within an exam."""
    row = row or {}
    defaults = defaults or {}
    for source in (row, defaults):
        if source.get("section_id") not in (None, ""):
            sid = _safe_int(source.get("section_id"))
            if sid is not None:
                sec = ExamSection.query.filter_by(id=sid, exam_id=exam_id).first()
                if sec:
                    return sid
    for source in (row, defaults):
        for key in ("section", "section_name", "section_title"):
            raw = source.get(key)
            if raw in (None, ""):
                continue
            name = str(raw).strip()[:200]
            sec = ExamSection.query.filter(
                ExamSection.exam_id == exam_id,
                ExamSection.title.ilike(name),
            ).first()
            if sec:
                return sec.id
    return None


def _extract_options(row: Dict[str, Any]) -> List[Tuple[str, str]]:
    options: List[Tuple[str, str]] = []
    for i, key in enumerate(OPTION_KEYS):
        if len(options) >= _MAX_OPTIONS:
            break
        val = (
            row.get(f"option_{key.lower()}")
            or row.get(f"option{key}")
            or row.get(key)
            or row.get(key.lower())
        )
        if val is not None and str(val).strip() != "":
            options.append((key, str(val).strip()[:_MAX_TEXT]))
    if not options and row.get("options"):
        raw = row["options"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = [x.strip() for x in raw.split("|") if x.strip()]
        if isinstance(raw, list):
            for i, val in enumerate(raw[:_MAX_OPTIONS]):
                if isinstance(val, dict):
                    k = val.get("option_key") or (
                        OPTION_KEYS[i] if i < len(OPTION_KEYS) else str(i)
                    )
                    options.append(
                        (str(k).upper()[:8], str(val.get("option_text", ""))[:_MAX_TEXT])
                    )
                else:
                    k = OPTION_KEYS[i] if i < len(OPTION_KEYS) else str(i)
                    options.append((k, str(val)[:_MAX_TEXT]))
    return options


def _validate_row(row: Dict[str, Any]) -> Optional[str]:
    if not isinstance(row, dict):
        return "invalid row"
    text = (row.get("question_text") or row.get("question") or "")
    if not str(text).strip():
        return "missing question_text"
    correct = row.get("correct_answer") or row.get("answer")
    if correct is None or str(correct).strip() == "":
        return "missing correct_answer"
    qtype = str(row.get("question_type") or "single_choice").strip().lower()
    if qtype and qtype not in QUESTION_TYPES and qtype != "single_choice":
        # will coerce later — not hard fail
        pass
    return None


def _create_from_row(
    row: Dict[str, Any],
    user_id: Optional[int],
    defaults: Optional[Dict[str, Any]] = None,
    *,
    skip_duplicates: bool = True,
    auto_create_taxonomy: bool = True,
) -> Tuple[Optional[Question], Optional[str], str]:
    """
    Returns (question|None, error|None, status)
    status: created | duplicate | error
    """
    defaults = defaults or {}
    if not isinstance(row, dict):
        return None, "invalid row", "error"

    err = _validate_row(row)
    if err:
        return None, err, "error"

    text = str(row.get("question_text") or row.get("question") or "").strip()[:_MAX_TEXT]
    qtype = str(
        row.get("question_type") or defaults.get("question_type") or "single_choice"
    ).strip().lower()
    if qtype not in QUESTION_TYPES:
        qtype = "single_choice"

    difficulty = _detect_difficulty(text, row.get("difficulty"))

    correct = row.get("correct_answer") or row.get("answer") or ""
    options = _extract_options(row)
    content_hash = compute_question_hash(
        text,
        [{"option_text": o[1]} for o in options],
        qtype,
        correct,
    )

    if skip_duplicates and content_hash:
        existing = Question.query.filter_by(content_hash=content_hash).first()
        if existing:
            # still allow mapping to exam/bank if requested
            bank_id = _safe_int(defaults.get("bank_id") or row.get("bank_id"))
            if bank_id and not existing.bank_id:
                existing.bank_id = bank_id
            exam_id = _resolve_exam_id(row, defaults)
            if exam_id:
                _map_to_exam(existing, exam_id, {**defaults, **row})
            return existing, "duplicate question", "duplicate"

    subject_id, chapter_id = _resolve_subject_chapter(
        row,
        defaults.get("subject_id"),
        defaults.get("chapter_id"),
        auto_create=auto_create_taxonomy,
    )
    topic_id = _resolve_topic(row, chapter_id, auto_create=auto_create_taxonomy)
    bank_id = _safe_int(row.get("bank_id") or defaults.get("bank_id"))

    marks = _safe_float(
        row.get("marks") if row.get("marks") not in (None, "") else defaults.get("marks"),
        1.0,
    )
    neg = _safe_float(
        row.get("negative_marks")
        if row.get("negative_marks") not in (None, "")
        else defaults.get("negative_marks"),
        0.0,
    )

    tags_raw = row.get("tags") or ""
    if isinstance(tags_raw, list):
        tag_list = [str(t).strip() for t in tags_raw if str(t).strip()]
        tags_str = ",".join(tag_list)[:512]
    else:
        tags_str = str(tags_raw)[:512]
        tag_list = [t.strip() for t in tags_str.split(",") if t.strip()]

    # Auto tags from metadata
    for key in ("pyq", "book", "practice", "year", "tier", "shift", "source"):
        if row.get(key) not in (None, "", False):
            tag_list.append(str(key))
    tag_list = list(dict.fromkeys(tag_list))

    correct_str = str(correct).strip()
    if qtype == "multiple_choice" and isinstance(correct, str) and "," in correct:
        keys = [k.strip().upper() for k in correct.split(",") if k.strip()]
        stored_correct = json.dumps(keys)
    elif qtype != "integer" and "," not in correct_str:
        stored_correct = correct_str.upper()
    else:
        stored_correct = correct_str

    year = _safe_int(row.get("year"))
    q = Question(
        subject_id=subject_id,
        chapter_id=chapter_id,
        bank_id=bank_id,
        topic_id=topic_id,
        content_hash=content_hash,
        version=1,
        question_type=qtype,
        difficulty=difficulty,
        question_text=text,
        question_html=_clip(row.get("question_html"), _MAX_TEXT),
        question_markdown=_clip(row.get("question_markdown") or row.get("markdown"), _MAX_TEXT),
        explanation=_clip(row.get("explanation"), _MAX_TEXT),
        explanation_html=_clip(row.get("explanation_html"), _MAX_TEXT),
        paragraph_text=_clip(row.get("paragraph_text") or row.get("paragraph"), _MAX_TEXT),
        image_url=_clip(row.get("image_url"), 512),
        marks=marks,
        negative_marks=neg,
        correct_answer=stored_correct,
        tags=",".join(tag_list)[:512],
        language=_clip(row.get("language") or "en", 32) or "en",
        created_by=user_id,
        status=str(row.get("status") or "active")[:32],
        year=year,
        shift=_clip(row.get("shift"), 64),
        tier=_clip(row.get("tier"), 64),
        source=_clip(row.get("source"), 255),
        is_pyq=bool(row.get("is_pyq") or row.get("pyq") or False),
        is_book=bool(row.get("is_book") or row.get("book") or False),
        is_practice=bool(row.get("is_practice") if row.get("is_practice") is not None else True),
        is_favorite=bool(row.get("is_favorite") or row.get("favorite") or False),
        is_active=str(row.get("status") or "active").lower() not in ("hidden", "archived"),
    )
    # Sanitize to valid UTF-8 to avoid psycopg2 decode error (Render issue)
    def _sanitize(s: str) -> str:
        if not s:
            return s
        try:
            return s.encode('utf-8', 'ignore').decode('utf-8', 'ignore')
        except Exception:
            return "".join(ch for ch in s if ord(ch) < 65535)[:_MAX_TEXT]

    q.question_text = _sanitize(q.question_text)
    if q.explanation:
        q.explanation = _sanitize(q.explanation)

    db.session.add(q)
    db.session.flush()

    if qtype != "integer":
        for i, (key, text_opt) in enumerate(options[:_MAX_OPTIONS]):
            try:
                db.session.add(
                    QuestionOption(
                        question_id=q.id,
                        option_key=str(key)[:8],
                        option_text=_sanitize(text_opt or ""),
                        order_index=i,
                    )
                )
            except Exception:
                continue

    # Tag mapping - SKIPPED for bulk import to avoid 4000 queries + OOM on Render free tier
    # Tags are stored as comma string in Question.tags column (enough for search)
    # If you need QuestionTag mapping, enable via env ENABLE_TAG_MAPPING=true
    # This saves ~3 queries per question = 3000 queries for 1000 questions
    try:
        enable_tag_map = False  # Disabled by default for bulk safety
        # import os
        # enable_tag_map = os.getenv("ENABLE_TAG_MAPPING", "false").lower() == "true"
        if enable_tag_map:
            for tag in _ensure_tags(tag_list):
                try:
                    if not QuestionTag.query.filter_by(question_id=q.id, tag_id=tag.id).first():
                        db.session.add(QuestionTag(question_id=q.id, tag_id=tag.id))
                except Exception:
                    continue
    except Exception:
        logger.exception("tag mapping skipped")

    exam_id = _resolve_exam_id(row, defaults)
    if exam_id:
        try:
            _map_to_exam(q, exam_id, {**defaults, **row})
        except Exception:
            logger.exception("map_to_exam skipped for bulk")

    return q, None, "created"


def _map_to_exam(question: Question, exam_id: int, defaults: Dict[str, Any]) -> None:
    exam = Exam.query.get(exam_id)
    if not exam:
        logger.warning("map_to_exam: exam_id=%s not found", exam_id)
        return
    if ExamQuestion.query.filter_by(exam_id=exam_id, question_id=question.id).first():
        return
    section_id = _resolve_section_id(exam_id, defaults, defaults)
    if section_id is None:
        section_id = _safe_int(defaults.get("section_id"))
        if section_id:
            sec = ExamSection.query.filter_by(id=section_id, exam_id=exam_id).first()
            if not sec:
                section_id = None
    if not section_id and exam.sections:
        section_id = exam.sections[0].id
    order = exam.exam_questions.count()
    db.session.add(
        ExamQuestion(
            exam_id=exam_id,
            section_id=section_id,
            question_id=question.id,
            order_index=order,
            marks=float(question.marks or exam.default_marks or 1),
            negative_marks=float(
                question.negative_marks
                if question.negative_marks is not None
                else (exam.default_negative_marks or 0)
            ),
        )
    )
    db.session.flush()
    exam.recalculate_totals()


def _run_import(
    rows: List[Dict[str, Any]],
    *,
    user_id: Optional[int],
    defaults: Dict[str, Any],
    source_type: str,
    file_name: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    job = ImportJob(
        user_id=user_id,
        source_type=source_type,
        status="processing",
        file_name=file_name,
        total_rows=len(rows),
        bank_id=_safe_int(defaults.get("bank_id")),
        exam_id=_safe_int(defaults.get("exam_id")),
        subject_id=_safe_int(defaults.get("subject_id")),
        chapter_id=_safe_int(defaults.get("chapter_id")),
    )
    db.session.add(job)
    db.session.flush()

    created: List[int] = []
    duplicates: List[int] = []
    errors: List[Dict[str, Any]] = []
    preview: List[Dict[str, Any]] = []

    skip_dup = bool(defaults.get("skip_duplicates", True))

    # Batch commit every 50 rows to avoid Render 512MB OOM + WORKER TIMEOUT
    BATCH_SIZE = 50
    for i, row in enumerate(rows):
        try:
            q, err, status = _create_from_row(
                row if isinstance(row, dict) else {},
                user_id,
                defaults,
                skip_duplicates=skip_dup,
            )
        except Exception as e:
            logger.exception("import row %s failed: %s", i + 1, str(e)[:200])
            errors.append({"row": i + 1, "error": "row processing failed"})
            # Rollback this row only, continue
            try:
                db.session.rollback()
            except Exception:
                pass
            continue

        if status == "error":
            errors.append({"row": i + 1, "error": err or "error"})
        elif status == "duplicate":
            duplicates.append(q.id if q else 0)
            if q:
                preview.append({"row": i + 1, "status": "duplicate", "question_id": q.id})
        else:
            created.append(q.id)
            preview.append({
                "row": i + 1,
                "status": "created",
                "question_id": q.id,
                "hash": q.content_hash,
            })

        # Commit batch to free memory and avoid timeout
        if (i + 1) % BATCH_SIZE == 0:
            try:
                db.session.commit()
                # Start new transaction for next batch
                db.session.begin()
            except Exception as e:
                logger.warning(f"Batch commit at row {i+1} failed: {e}")
                try:
                    db.session.rollback()
                except Exception:
                    pass

    if dry_run:
        db.session.rollback()
        # recreate job as cancelled dry-run log without inserts
        job = ImportJob(
            user_id=user_id,
            source_type=source_type,
            status="preview",
            file_name=file_name,
            total_rows=len(rows),
            success_count=len(created),
            error_count=len(errors),
            duplicate_count=len(duplicates),
            bank_id=_safe_int(defaults.get("bank_id")),
            exam_id=_safe_int(defaults.get("exam_id")),
            subject_id=_safe_int(defaults.get("subject_id")),
            chapter_id=_safe_int(defaults.get("chapter_id")),
            errors_json=json.dumps(errors[:_MAX_ERRORS_STORED]),
            meta_json=json.dumps({"dry_run": True, "preview": preview[:50]}),
            completed_at=_utcnow(),
        )
        db.session.add(job)
        db.session.commit()
        return {
            "message": "Preview only — no questions saved",
            "job": job.to_dict(),
            "created_ids": [],
            "duplicate_ids": duplicates,
            "errors": errors,
            "success_count": len(created),
            "error_count": len(errors),
            "duplicate_count": len(duplicates),
            "preview": preview[:100],
        }

    job.success_count = len(created)
    job.error_count = len(errors)
    job.duplicate_count = len(duplicates)
    job.status = "completed" if created or duplicates else "failed"
    job.errors_json = json.dumps(errors[:_MAX_ERRORS_STORED])
    job.meta_json = json.dumps({"created_ids": created[:200], "duplicate_ids": duplicates[:200]})
    job.completed_at = _utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("import commit failed")
        return {
            "message": "Import commit failed — rolled back",
            "created_ids": [],
            "errors": errors + [{"row": 0, "error": "transaction rollback"}],
            "success_count": 0,
            "error_count": len(errors) + 1,
            "duplicate_count": 0,
        }

    return {
        "message": f"Imported {len(created)} questions",
        "job": job.to_dict(),
        "created_ids": created,
        "duplicate_ids": duplicates,
        "errors": errors,
        "success_count": len(created),
        "error_count": len(errors),
        "duplicate_count": len(duplicates),
    }


@imports_bp.post("/questions/json")
@roles_required("admin")
def import_questions_json():
    from app.services.feature_flags import is_enabled

    if not is_enabled("ENABLE_IMPORT", True):
        return jsonify({"error": "Import feature is disabled"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    rows = data.get("questions") or data.get("items") or []
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "questions array required"}), 400
    if len(rows) > _MAX_JSON_ROWS:
        return jsonify({"error": f"At most {_MAX_JSON_ROWS} questions per import"}), 400

    defaults = {
        "subject_id": _safe_int(data.get("subject_id")),
        "chapter_id": _safe_int(data.get("chapter_id")),
        "bank_id": _safe_int(data.get("bank_id")),
        "exam_id": _safe_int(data.get("exam_id")),
        # Allow top-level exam name for whole batch
        "exam": data.get("exam") or data.get("exam_name") or data.get("exam_title"),
        "section_id": _safe_int(data.get("section_id")),
        "section": data.get("section") or data.get("section_name"),
        "marks": data.get("marks"),
        "negative_marks": data.get("negative_marks"),
        "question_type": data.get("question_type"),
        "skip_duplicates": bool(data.get("skip_duplicates", True)),
    }
    # Resolve batch-level exam name → id once (optional convenience)
    batch_exam = _resolve_exam_id({}, defaults)
    if batch_exam and not defaults.get("exam_id"):
        defaults["exam_id"] = batch_exam

    dry_run = bool(data.get("preview") or data.get("dry_run"))
    result = _run_import(
        rows,
        user_id=_identity_int(),
        defaults=defaults,
        source_type="json",
        dry_run=dry_run,
    )
    code = 200 if dry_run else (201 if result.get("success_count") or result.get("duplicate_count") else 400)
    return jsonify(result), code


@imports_bp.post("/questions/preview")
@roles_required("admin")
def preview_questions_json():
    """Validate + hash preview without committing questions."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    data = dict(data)
    data["preview"] = True
    # Reuse json importer
    request_json = data
    rows = request_json.get("questions") or request_json.get("items") or []
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "questions array required"}), 400
    defaults = {
        "subject_id": _safe_int(data.get("subject_id")),
        "chapter_id": _safe_int(data.get("chapter_id")),
        "bank_id": _safe_int(data.get("bank_id")),
        "exam_id": _safe_int(data.get("exam_id")),
        "exam": data.get("exam") or data.get("exam_name") or data.get("exam_title"),
        "section": data.get("section") or data.get("section_name"),
        "skip_duplicates": True,
    }
    batch_exam = _resolve_exam_id({}, defaults)
    if batch_exam and not defaults.get("exam_id"):
        defaults["exam_id"] = batch_exam
    result = _run_import(
        rows[: min(len(rows), 200)],
        user_id=_identity_int(),
        defaults=defaults,
        source_type="json",
        dry_run=True,
    )
    return jsonify(result)


@imports_bp.post("/questions/csv")
@roles_required("admin")
def import_questions_csv():
    from app.services.feature_flags import is_enabled

    if not is_enabled("ENABLE_IMPORT", True):
        return jsonify({"error": "Import feature is disabled"}), 403
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400

    raw = f.read(_MAX_CSV_BYTES + 1)
    if len(raw) > _MAX_CSV_BYTES:
        return jsonify({"error": "CSV file too large"}), 413

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            return jsonify({"error": "Unable to decode CSV file"}), 400

    if "\x00" in text:
        return jsonify({"error": "Invalid CSV content"}), 400

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return jsonify({"error": "CSV header row required"}), 400

    rows: List[Dict[str, Any]] = []
    for i, row in enumerate(reader):
        if i >= _MAX_CSV_ROWS:
            break
        nrow = {
            (k or "").strip().lower(): (v.strip() if isinstance(v, str) else v)
            for k, v in (row or {}).items()
        }
        rows.append(nrow)

    defaults = {
        "subject_id": request.form.get("subject_id", type=int),
        "chapter_id": request.form.get("chapter_id", type=int),
        "bank_id": request.form.get("bank_id", type=int),
        "exam_id": request.form.get("exam_id", type=int),
        "exam": request.form.get("exam") or request.form.get("exam_name"),
        "section_id": request.form.get("section_id", type=int),
        "section": request.form.get("section") or request.form.get("section_name"),
        "marks": request.form.get("marks", type=float),
        "negative_marks": request.form.get("negative_marks", type=float),
        "question_type": request.form.get("question_type"),
        "skip_duplicates": request.form.get("skip_duplicates", "true").lower() != "false",
    }
    batch_exam = _resolve_exam_id({}, defaults)
    if batch_exam and not defaults.get("exam_id"):
        defaults["exam_id"] = batch_exam
    dry_run = request.form.get("preview", "false").lower() in ("1", "true", "yes")
    result = _run_import(
        rows,
        user_id=_identity_int(),
        defaults=defaults,
        source_type="csv",
        file_name=getattr(f, "filename", None),
        dry_run=dry_run,
    )
    code = 200 if dry_run else (201 if result.get("success_count") or result.get("duplicate_count") else 400)
    return jsonify(result), code


@imports_bp.get("/questions/template")
@jwt_required()
def csv_template():
    header = (
        "question_text,option_a,option_b,option_c,option_d,correct_answer,"
        "explanation,subject,chapter,topic,difficulty,marks,negative_marks,"
        "question_type,tags,exam,section,year,tier,shift,source,is_pyq\n"
    )
    sample = (
        '"What is 2+2?",3,4,5,6,B,"Basic addition",Quantitative Aptitude,Number System,'
        'Addition,easy,2,0.5,single_choice,"math","SSC CHSL Style Mock Test — Demo",'
        '"Quantitative Aptitude",2024,tier1,shift1,SSC,false\n'
    )
    return jsonify({
        "filename": "questions_template.csv",
        "content": header + sample,
        "columns": [
            "question_text", "option_a", "option_b", "option_c", "option_d",
            "correct_answer", "explanation", "subject", "chapter", "topic",
            "difficulty", "marks", "negative_marks", "question_type", "tags",
            "exam", "section", "year", "tier", "shift", "source", "is_pyq",
        ],
        "notes": [
            "exam = exam title (or use exam_id). Leave empty to keep question only in bank.",
            "section = section title inside that exam (optional).",
            "Or set exam once at JSON root: { \"exam\": \"My Mock\", \"questions\": [...] }",
        ],
    })


@imports_bp.get("/jobs")
@roles_required("admin")
def list_import_jobs():
    jobs = ImportJob.query.order_by(ImportJob.id.desc()).limit(100).all()
    return jsonify({"items": [j.to_dict() for j in jobs], "total": len(jobs)})


@imports_bp.get("/jobs/<int:job_id>")
@roles_required("admin")
def get_import_job(job_id):
    job = ImportJob.query.get_or_404(job_id)
    return jsonify(job.to_dict())


# ============================================================
# KNOWLEDGE ENGINE - Internal Brain - AI Import Endpoints
# ============================================================

def _extract_text_from_file(file_bytes: bytes, filename: str, ext: str) -> str:
    """
    Layer 1: INTAKE - Accept ANY format and extract raw text
    Supports PDF, DOCX, CSV, JSON, HTML, MD, TXT with graceful fallback
    """
    ext = (ext or "").lower().strip(".")
    text = ""

    if ext == "pdf":
        # Try PyMuPDF (fitz) if available, else pypdf, else fallback
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            text = "\n".join([page.get_text() for page in doc])
            if text.strip():
                return text
        except Exception:
            pass
        try:
            # Try pypdf as fallback
            import io as _io
            from pypdf import PdfReader
            reader = PdfReader(_io.BytesIO(file_bytes))
            text = "\n".join([p.extract_text() or "" for p in reader.pages])
            if text.strip():
                return text
        except Exception:
            pass
        # Final fallback - try decode (for text-based PDFs)
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
            if len(text.strip()) > 100:
                return text
        except Exception:
            pass
        return ""

    elif ext == "docx":
        try:
            import docx
            import io as _io
            doc = docx.Document(_io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs])
            if text.strip():
                return text
        except Exception:
            pass
        # Fallback decode
        try:
            return file_bytes.decode('utf-8', errors='ignore')
        except Exception:
            return ""

    elif ext in ("json",):
        try:
            data = json.loads(file_bytes.decode('utf-8', errors='ignore'))
            # If it's already structured questions, return as JSON string for pipeline
            # Pipeline will handle blocks extraction
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return file_bytes.decode('utf-8', errors='ignore')

    elif ext in ("csv", "txt", "md", "html", "htm", "jsonl"):
        try:
            return file_bytes.decode('utf-8-sig', errors='ignore')
        except Exception:
            try:
                return file_bytes.decode('latin-1', errors='ignore')
            except Exception:
                return ""

    elif ext in ("png", "jpg", "jpeg", "webp", "bmp", "tiff"):
        # OCR path - requires pytesseract + Pillow
        try:
            from PIL import Image
            import pytesseract
            import io as _io
            img = Image.open(_io.BytesIO(file_bytes))
            text = pytesseract.image_to_string(img)
            # Also try Hindi if available
            if not text.strip():
                try:
                    text = pytesseract.image_to_string(img, lang='eng+hin')
                except Exception:
                    pass
            return text
        except Exception as e:
            logger.warning(f"OCR not available for image {filename}: {e}")
            return ""

    else:
        # Generic try utf-8
        try:
            return file_bytes.decode('utf-8-sig', errors='ignore')
        except Exception:
            try:
                return file_bytes.decode('latin-1', errors='ignore')
            except Exception:
                return ""


def _save_canonical_to_db(
    canonical,
    user_id: Optional[int],
    defaults: Dict[str, Any],
    skip_duplicates: bool = True,
) -> Tuple[Optional[Question], str, Optional[int]]:
    """
    Save canonical question to DB with appearance history merging
    Returns (question, status, duplicate_of_id)
    status: created | duplicate | error
    """
    from app.services.knowledge_engine.deduplicator import normalize_for_comparison

    # Check duplicate via knowledge engine deduplicator
    fingerprint = canonical.fingerprint_hash
    semantic = canonical.semantic_hash
    normalized = canonical.normalized_question

    # If duplicate, merge appearance instead of creating
    if skip_duplicates and canonical.duplicate_info.get("is_duplicate"):
        dup_id = canonical.duplicate_info.get("duplicate_of")
        if dup_id:
            existing = Question.query.get(dup_id)
            if existing:
                # Merge appearance history
                if QuestionAppearance and canonical.appearance_history:
                    try:
                        for app in canonical.appearance_history:
                            # Avoid duplicate appearance
                            exists_app = QuestionAppearance.query.filter_by(
                                question_id=existing.id,
                                exam_name=app.get("exam_name"),
                                exam_year=app.get("exam_year"),
                                source_book=app.get("source_book"),
                                page_number=app.get("page_number"),
                            ).first()
                            if not exists_app:
                                new_app = QuestionAppearance(
                                    question_id=existing.id,
                                    exam_name=app.get("exam_name"),
                                    exam_year=app.get("exam_year"),
                                    exam_date=app.get("exam_date"),
                                    shift=app.get("shift"),
                                    session=app.get("session"),
                                    organization=app.get("organization"),
                                    board=app.get("board"),
                                    source_book=app.get("source_book"),
                                    source_type=app.get("source_type", "book"),
                                    page_number=app.get("page_number"),
                                    question_number=app.get("question_number"),
                                    language_detected=app.get("language_detected", "en"),
                                    source_hash=app.get("source_hash"),
                                )
                                db.session.add(new_app)
                        # Update appearance count
                        try:
                            existing.appearance_count = (existing.appearance_count or 0) + 1
                        except Exception:
                            pass
                        db.session.flush()
                    except Exception as e:
                        logger.warning(f"appearance merge failed for q={existing.id}: {e}")
                return existing, "duplicate", dup_id

    # Resolve taxonomy with auto-create
    subject_name = canonical.classification.get("subject")
    chapter_name = canonical.classification.get("chapter")
    topic_name = canonical.classification.get("topic")

    subject_id = defaults.get("subject_id")
    chapter_id = defaults.get("chapter_id")

    # Auto-create subject if needed
    if not subject_id and subject_name:
        s = Subject.query.filter(Subject.name.ilike(subject_name.strip()[:200])).first()
        if not s and defaults.get("auto_create", True):
            s = Subject(name=subject_name.strip()[:200], is_active=True)
            db.session.add(s)
            db.session.flush()
        if s:
            subject_id = s.id

    if not chapter_id and chapter_name and subject_id:
        c = Chapter.query.filter(
            Chapter.subject_id == subject_id,
            Chapter.name.ilike(chapter_name.strip()[:200]),
        ).first()
        if not c and defaults.get("auto_create", True):
            c = Chapter(subject_id=subject_id, name=chapter_name.strip()[:200], is_active=True)
            db.session.add(c)
            db.session.flush()
        if c:
            chapter_id = c.id

    # If still no subject/chapter, use defaults or keep null (question stays in bank)
    if not subject_id:
        subject_id = _safe_int(defaults.get("subject_id"))
    if not chapter_id:
        chapter_id = _safe_int(defaults.get("chapter_id"))

    # Build Question model payload
    qtype = canonical.question_type
    if qtype not in QUESTION_TYPES:
        # Map extended types to existing
        mapping = {
            "assertion_reason": "single_choice",
            "statement_based": "single_choice",
            "match_the_following": "single_choice",
            "fill_blank": "single_choice",
            "true_false": "single_choice",
            "integer": "integer",
            "paragraph": "paragraph",
            "image_based": "image",
            "multiple_choice": "multiple_choice",
        }
        qtype = mapping.get(qtype, "single_choice")

    # Correct answer
    correct = canonical.correct_answer
    if correct is None:
        # NO HALLUCINATION - keep empty string but mark needs_review
        stored_correct = ""
    elif isinstance(correct, list):
        stored_correct = json.dumps([str(x).upper() for x in correct])
    else:
        stored_correct = str(correct).strip().upper() or ""

    # Marks
    marks = _safe_float(defaults.get("marks") or 2.0, 2.0)
    neg_marks = _safe_float(defaults.get("negative_marks") or 0.5, 0.5)

    # Tags
    tags_str = ",".join(canonical.tags[:15])[:512]

    # Create Question
    try:
        q = Question(
            subject_id=subject_id,
            chapter_id=chapter_id,
            content_hash=fingerprint,
            version=1,
            question_type=qtype,
            difficulty=canonical.classification.get("difficulty", "medium") if canonical.classification.get("difficulty") in DIFFICULTIES else "medium",
            question_text=canonical.question_text[:_MAX_TEXT],
            explanation=canonical.explanation[:_MAX_TEXT] if canonical.explanation else None,
            paragraph_text=canonical.paragraph.get("text")[:_MAX_TEXT] if canonical.paragraph else None,
            marks=marks,
            negative_marks=neg_marks,
            correct_answer=stored_correct,
            tags=tags_str,
            language=canonical.language_detected[:32] if canonical.language_detected else "en",
            is_active=True,
            status="needs_review" if canonical.needs_review else "active",
            source=canonical.metadata.get("source_book") or canonical.metadata.get("exam_name") or defaults.get("source_book") or "AI Import",
            year=_safe_int(canonical.metadata.get("exam_year") or defaults.get("exam_year")),
            shift=_clip(canonical.metadata.get("shift"), 64),
            tier=_clip(canonical.metadata.get("tier") or "Tier-1", 64),
            is_pyq=bool(canonical.metadata.get("exam_name")),
            is_book=bool(canonical.metadata.get("source_book") and "book" in str(canonical.metadata.get("source_book")).lower()),
            is_practice=True,
            created_by=user_id,
        )

        # Try to set extended fields if columns exist (via schema_upgrade)
        try:
            q.raw_text = canonical.raw_text[:20000] if hasattr(q, 'raw_text') else None
        except Exception:
            pass
        try:
            if hasattr(q, 'normalized_question'):
                q.normalized_question = canonical.normalized_question[:10000]
        except Exception:
            pass
        try:
            if hasattr(q, 'semantic_hash'):
                q.semantic_hash = semantic
        except Exception:
            pass
        try:
            if hasattr(q, 'source_hash'):
                q.source_hash = canonical.source_hash
        except Exception:
            pass
        try:
            if hasattr(q, 'qid'):
                q.qid = canonical.qid
        except Exception:
            pass
        try:
            if hasattr(q, 'semantic_summary'):
                q.semantic_summary = canonical.semantic_summary[:1000]
        except Exception:
            pass
        try:
            if hasattr(q, 'classification_json'):
                q.classification_json = json.dumps(canonical.classification, ensure_ascii=False)
        except Exception:
            pass
        try:
            if hasattr(q, 'confidence_score'):
                q.confidence_score = canonical.confidence_score
        except Exception:
            pass
        try:
            if hasattr(q, 'needs_review'):
                q.needs_review = canonical.needs_review
        except Exception:
            pass
        try:
            if hasattr(q, 'review_reason'):
                q.review_reason = ",".join(canonical.review_reasons)[:255]
        except Exception:
            pass
        try:
            if hasattr(q, 'search_tokens'):
                q.search_tokens = " ".join(canonical.keywords)[:2000]
        except Exception:
            pass
        try:
            if hasattr(q, 'embeddings_text'):
                q.embeddings_text = canonical.semantic_summary[:2000]
        except Exception:
            pass
        try:
            if hasattr(q, 'question_family'):
                q.question_family = canonical.classification.get("question_family", "")[:128]
        except Exception:
            pass
        try:
            if hasattr(q, 'pattern'):
                q.pattern = canonical.classification.get("pattern", "")[:128]
        except Exception:
            pass
        try:
            if hasattr(q, 'bloom_taxonomy'):
                q.bloom_taxonomy = canonical.classification.get("bloom_taxonomy", "")[:32]
        except Exception:
            pass
        try:
            if hasattr(q, 'expected_time_seconds'):
                q.expected_time_seconds = canonical.classification.get("expected_time_seconds")
        except Exception:
            pass

        db.session.add(q)
        db.session.flush()

        # Save options
        if qtype != "integer" and canonical.options:
            for idx, opt in enumerate(canonical.options[:_MAX_OPTIONS]):
                db.session.add(
                    QuestionOption(
                        question_id=q.id,
                        option_key=str(opt.get("option_key", "A"))[:8],
                        option_text=str(opt.get("option_text", ""))[:_MAX_TEXT],
                        order_index=idx,
                    )
                )

        # Save appearance history
        if QuestionAppearance and canonical.appearance_history:
            for app in canonical.appearance_history:
                try:
                    new_app = QuestionAppearance(
                        question_id=q.id,
                        exam_name=app.get("exam_name"),
                        exam_year=app.get("exam_year"),
                        exam_date=app.get("exam_date"),
                        shift=app.get("shift"),
                        session=app.get("session"),
                        organization=app.get("organization"),
                        board=app.get("board"),
                        source_book=app.get("source_book"),
                        source_type=app.get("source_type", "book"),
                        page_number=app.get("page_number"),
                        question_number=app.get("question_number"),
                        language_detected=app.get("language_detected", "en"),
                        source_hash=app.get("source_hash"),
                    )
                    db.session.add(new_app)
                except Exception:
                    continue

        # Map to exam if exam_id provided
        exam_id = _safe_int(defaults.get("exam_id"))
        if not exam_id and canonical.metadata.get("exam_name"):
            exam_id = _resolve_exam_id({"exam": canonical.metadata.get("exam_name")}, defaults)
        if exam_id:
            _map_to_exam(q, exam_id, defaults)

        db.session.flush()
        return q, "created", None

    except Exception as e:
        logger.exception(f"Failed to save canonical qid={canonical.qid}: {e}")
        db.session.rollback()
        return None, "error", None


def _run_ai_import(
    canonical_questions: List,
    user_id: Optional[int],
    defaults: Dict[str, Any],
    source_type: str,
    file_name: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Run AI import with appearance merging and duplicate safety
    """
    # Create KnowledgeIngestionJob if model exists
    k_job = None
    if KnowledgeIngestionJob:
        try:
            k_job = KnowledgeIngestionJob(
                user_id=user_id,
                source_type=source_type,
                file_name=file_name,
                file_hash=hashlib.sha256((file_name or "").encode()).hexdigest()[:16] if file_name else None,
                status="processing",
                total_blocks_found=len(canonical_questions),
                source_book=defaults.get("source_book"),
                exam_name=defaults.get("exam_name"),
                exam_year=_safe_int(defaults.get("exam_year")),
            )
            db.session.add(k_job)
            db.session.flush()
        except Exception:
            k_job = None

    created = []
    duplicates = []
    needs_review_list = []
    errors = []
    preview = []

    if dry_run:
        # No DB write, just preview
        for idx, canon in enumerate(canonical_questions[:200]):
            preview.append(canon.to_frontend_compatible())
        if k_job:
            try:
                k_job.status = "preview"
                k_job.questions_created = 0
                k_job.duplicates_found = 0
                k_job.needs_review = len([c for c in canonical_questions if c.needs_review])
                k_job.preview_json = json.dumps(preview[:20], ensure_ascii=False)
                k_job.completed_at = _utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()

        return {
            "message": "AI Preview - no questions saved",
            "knowledge_job": k_job.to_dict() if k_job else None,
            "total_blocks_found": len(canonical_questions),
            "questions_created": 0,
            "duplicates_found": 0,
            "needs_review": len([c for c in canonical_questions if c.needs_review]),
            "questions": [c.to_full_knowledge_object() for c in canonical_questions[:50]],
            "preview": preview[:50],
            "errors": [],
        }

    # Actual import with transaction per batch
    skip_dup = bool(defaults.get("skip_duplicates", True))

    for idx, canon in enumerate(canonical_questions):
        # Safety: max per batch
        if idx >= _MAX_AI_QUESTIONS_PER_BATCH:
            errors.append({"row": idx + 1, "error": f"Batch limit {_MAX_AI_QUESTIONS_PER_BATCH} reached"})
            break
        try:
            q, status, dup_of = _save_canonical_to_db(
                canon, user_id, defaults, skip_duplicates=skip_dup
            )
            if status == "created" and q:
                created.append(q.id)
                if canon.needs_review:
                    needs_review_list.append(q.id)
                preview.append({"row": idx + 1, "status": "created", "question_id": q.id, "qid": canon.qid})
            elif status == "duplicate":
                duplicates.append(q.id if q else 0)
                preview.append({"row": idx + 1, "status": "duplicate", "question_id": q.id if q else None, "duplicate_of": dup_of, "qid": canon.qid})
            else:
                errors.append({"row": idx + 1, "error": "save failed"})
        except Exception as e:
            logger.exception(f"AI import row {idx} failed")
            errors.append({"row": idx + 1, "error": str(e)[:200]})

    # Commit
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("AI import commit failed")
        return {
            "message": "AI Import commit failed",
            "total_blocks_found": len(canonical_questions),
            "questions_created": 0,
            "duplicates_found": 0,
            "needs_review": 0,
            "errors": errors + [{"row": 0, "error": "transaction rollback"}],
            "success_count": 0,
            "error_count": len(errors) + 1,
        }

    # Update job
    if k_job:
        try:
            k_job.status = "completed" if (created or duplicates) else "failed"
            k_job.questions_created = len(created)
            k_job.duplicates_found = len(duplicates)
            k_job.needs_review = len(needs_review_list)
            k_job.errors_count = len(errors)
            k_job.errors_json = json.dumps(errors[:_MAX_ERRORS_STORED], ensure_ascii=False)
            k_job.preview_json = json.dumps(preview[:50], ensure_ascii=False)
            k_job.completed_at = _utcnow()
            db.session.add(k_job)
            db.session.commit()
        except Exception:
            db.session.rollback()

    return {
        "message": f"AI Engine: {len(created)} created, {len(duplicates)} duplicates merged, {len(needs_review_list)} needs review",
        "knowledge_job": k_job.to_dict() if k_job else None,
        "total_blocks_found": len(canonical_questions),
        "questions_created": len(created),
        "duplicates_found": len(duplicates),
        "needs_review": len(needs_review_list),
        "created_ids": created,
        "duplicate_ids": duplicates,
        "needs_review_ids": needs_review_list,
        "errors": errors,
        "success_count": len(created),
        "error_count": len(errors),
        "duplicate_count": len(duplicates),
        "preview": preview[:100],
    }


# ----- NEW: AI Knowledge Engine Routes -----

@imports_bp.post("/questions/ai")
@roles_required("admin")
def import_questions_ai():
    """
    AI Knowledge Engine - Accepts ANY educational content
    Body: { raw_text, source_book, exam_name, exam_year, source_type, marks, negative_marks, skip_duplicates, preview }
    Supports: Pinnacle books, Kiran, Lucent, Testbook PDFs (OCR'd text), Adda247 screenshots (OCR'd), typed text, broken OCR, mixed Hindi-English
    """
    from app.services.feature_flags import is_enabled

    if not is_enabled("ENABLE_IMPORT", True):
        return jsonify({"error": "Import feature is disabled"}), 403

    if not KNOWLEDGE_ENGINE_AVAILABLE or not knowledge_engine:
        return jsonify({"error": "Knowledge Engine not available - check server logs"}), 500

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON body required"}), 400

    raw_text = data.get("raw_text") or data.get("content") or data.get("text") or ""
    if isinstance(raw_text, dict):
        # Might be questions array already
        raw_text = json.dumps(raw_text, ensure_ascii=False)

    if not raw_text or not str(raw_text).strip():
        # Support legacy: questions array? Convert to raw text blocks
        rows = data.get("questions") or data.get("items") or []
        if rows and isinstance(rows, list):
            # If already structured, use old path but via knowledge engine
            raw_text = "\n\n".join([
                f"Q. {r.get('question_text','')}\nA) {r.get('option_a','')}\nB) {r.get('option_b','')}\nC) {r.get('option_c','')}\nD) {r.get('option_d','')}\nAnswer: {r.get('correct_answer','')}"
                for r in rows[:500]
            ])
        else:
            return jsonify({"error": "raw_text or content field required - paste any educational content"}), 400

    if len(raw_text) > _MAX_AI_TEXT_BYTES:
        return jsonify({"error": f"Text too large - max {_MAX_AI_TEXT_BYTES} bytes"}), 413

    # Source metadata - never hallucinate, keep null if missing
    defaults = {
        "source_book": _clip(data.get("source_book") or data.get("book"), 255),
        "exam_name": _clip(data.get("exam_name") or data.get("exam"), 255),
        "exam_year": _safe_int(data.get("exam_year") or data.get("year")),
        "exam": data.get("exam") or data.get("exam_name"),
        "subject_id": _safe_int(data.get("subject_id")),
        "chapter_id": _safe_int(data.get("chapter_id")),
        "bank_id": _safe_int(data.get("bank_id")),
        "exam_id": _safe_int(data.get("exam_id")),
        "marks": _safe_float(data.get("marks"), 2.0),
        "negative_marks": _safe_float(data.get("negative_marks"), 0.5),
        "skip_duplicates": bool(data.get("skip_duplicates", True)),
        "auto_create": bool(data.get("auto_create", True)),
    }

    # Resolve exam_id from name if needed
    batch_exam = _resolve_exam_id({}, defaults)
    if batch_exam and not defaults.get("exam_id"):
        defaults["exam_id"] = batch_exam

    dry_run = bool(data.get("preview") or data.get("dry_run") or data.get("is_preview"))

    try:
        # Layer 1-6 via pipeline
        canonical_list = knowledge_engine.process_document(
            content=str(raw_text),
            source_meta={
                "source_book": defaults.get("source_book"),
                "source_type": data.get("source_type", "typed"),
                "exam_name": defaults.get("exam_name"),
                "exam_year": defaults.get("exam_year"),
                "file_name": defaults.get("source_book") or "raw_text_input",
                "marks": defaults.get("marks"),
                "negative_marks": defaults.get("negative_marks"),
            }
        )
    except Exception as e:
        logger.exception("Knowledge Engine process_document failed")
        return jsonify({"error": f"AI Engine failed: {str(e)[:300]}"}), 500

    if not canonical_list:
        return jsonify({
            "message": "No question blocks detected - check input format",
            "total_blocks_found": 0,
            "questions_created": 0,
            "duplicates_found": 0,
            "needs_review": 0,
            "questions": [],
        }), 200

    # If preview only, don't save
    if dry_run:
        result = _run_ai_import(
            canonical_list,
            _identity_int(),
            defaults,
            source_type=data.get("source_type", "typed"),
            file_name=defaults.get("source_book") or "preview",
            dry_run=True,
        )
        return jsonify(result), 200

    result = _run_ai_import(
        canonical_list,
        _identity_int(),
        defaults,
        source_type=data.get("source_type", "typed"),
        file_name=defaults.get("source_book") or "ai_text_import",
        dry_run=False,
    )

    # Return in requested format - full knowledge objects + frontend compatible
    # Include questions with full details for frontend preview
    full_result = {
        **result,
        "questions": [c.to_full_knowledge_object() for c in canonical_list[:100]],  # limit response
    }

    code = 201 if result.get("questions_created") or result.get("duplicates_found") else 400
    return jsonify(full_result), code


@imports_bp.post("/questions/ai/file")
@roles_required("admin")
def import_questions_ai_file():
    """
    AI Engine File Import - Accepts ANY file type
    PDF, image, screenshot, DOCX, CSV, JSON, HTML, MD, TXT, mixed Hindi-English, broken OCR
    FormData: file, source_book, exam_name, exam_year, source_type, preview
    """
    from app.services.feature_flags import is_enabled

    if not is_enabled("ENABLE_IMPORT", True):
        return jsonify({"error": "Import feature is disabled"}), 403

    if not KNOWLEDGE_ENGINE_AVAILABLE or not knowledge_engine:
        return jsonify({"error": "Knowledge Engine not available"}), 500

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required - upload PDF, DOCX, image, CSV, JSON, TXT, MD"}), 400

    raw_bytes = f.read(_MAX_AI_FILE_BYTES + 1)
    if len(raw_bytes) > _MAX_AI_FILE_BYTES:
        return jsonify({"error": f"File too large - max {_MAX_AI_FILE_BYTES} bytes"}), 413

    filename = getattr(f, "filename", "upload") or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    # Layer 1: INTAKE - extract text from any format
    try:
        extracted_text = _extract_text_from_file(raw_bytes, filename, ext)
    except Exception as e:
        logger.exception(f"File text extraction failed for {filename}")
        return jsonify({"error": f"Could not extract text from file: {str(e)[:200]}"}), 400

    if not extracted_text or len(extracted_text.strip()) < 20:
        return jsonify({
            "error": "File seems empty or OCR failed. For scanned PDFs/images, ensure OCR dependencies (pytesseract, Pillow) are installed on server.",
            "filename": filename,
            "extracted_len": len(extracted_text) if extracted_text else 0,
        }), 400

    defaults = {
        "source_book": _clip(request.form.get("source_book") or filename, 255),
        "exam_name": _clip(request.form.get("exam_name") or request.form.get("exam"), 255),
        "exam_year": _safe_int(request.form.get("exam_year") or request.form.get("year")),
        "exam": request.form.get("exam") or request.form.get("exam_name"),
        "subject_id": request.form.get("subject_id", type=int),
        "chapter_id": request.form.get("chapter_id", type=int),
        "bank_id": request.form.get("bank_id", type=int),
        "exam_id": request.form.get("exam_id", type=int),
        "marks": request.form.get("marks", type=float) or 2.0,
        "negative_marks": request.form.get("negative_marks", type=float) or 0.5,
        "skip_duplicates": request.form.get("skip_duplicates", "true").lower() != "false",
        "auto_create": request.form.get("auto_create", "true").lower() != "false",
    }

    batch_exam = _resolve_exam_id({}, defaults)
    if batch_exam and not defaults.get("exam_id"):
        defaults["exam_id"] = batch_exam

    dry_run = request.form.get("preview", "false").lower() in ("1", "true", "yes")

    try:
        canonical_list = knowledge_engine.process_document(
            content=extracted_text,
            source_meta={
                "source_book": defaults.get("source_book"),
                "source_type": request.form.get("source_type", ext if ext in ("pdf","docx","csv","json") else "file"),
                "exam_name": defaults.get("exam_name"),
                "exam_year": defaults.get("exam_year"),
                "file_name": filename,
                "marks": defaults.get("marks"),
                "negative_marks": defaults.get("negative_marks"),
            },
            file_type=ext,
        )
    except Exception as e:
        logger.exception("Knowledge Engine file processing failed")
        return jsonify({"error": f"AI Engine failed: {str(e)[:300]}"}), 500

    if not canonical_list:
        return jsonify({
            "message": "No question blocks detected in file",
            "filename": filename,
            "extracted_preview": extracted_text[:1000],
            "total_blocks_found": 0,
            "questions_created": 0,
            "duplicates_found": 0,
            "needs_review": 0,
        }), 200

    result = _run_ai_import(
        canonical_list,
        _identity_int(),
        defaults,
        source_type=request.form.get("source_type", ext),
        file_name=filename,
        dry_run=dry_run,
    )

    result["filename"] = filename
    result["extracted_preview"] = extracted_text[:2000]
    result["questions"] = [c.to_full_knowledge_object() for c in canonical_list[:100]]

    code = 200 if dry_run else (201 if result.get("questions_created") or result.get("duplicates_found") else 400)
    return jsonify(result), code


@imports_bp.post("/questions/ai/preview")
@roles_required("admin")
def preview_questions_ai():
    """
    Preview without saving - validates and shows how AI parses content
    """
    data = request.get_json(silent=True) or {}
    data["preview"] = True
    # Reuse ai endpoint logic with dry_run
    return import_questions_ai()


@imports_bp.get("/knowledge/jobs")
@roles_required("admin")
def list_knowledge_jobs():
    if not KnowledgeIngestionJob:
        return jsonify({"items": [], "total": 0})
    jobs = KnowledgeIngestionJob.query.order_by(KnowledgeIngestionJob.id.desc()).limit(100).all()
    return jsonify({"items": [j.to_dict() for j in jobs], "total": len(jobs)})


@imports_bp.get("/knowledge/jobs/<int:job_id>")
@roles_required("admin")
def get_knowledge_job(job_id):
    if not KnowledgeIngestionJob:
        return jsonify({"error": "Knowledge Engine not available"}), 500
    job = KnowledgeIngestionJob.query.get_or_404(job_id)
    return jsonify(job.to_dict())


@imports_bp.get("/knowledge/question/<int:question_id>/appearances")
@roles_required("admin")
def get_question_appearances(question_id):
    if not QuestionAppearance:
        return jsonify({"items": [], "total": 0})
    q = Question.query.get_or_404(question_id)
    apps = QuestionAppearance.query.filter_by(question_id=q.id).order_by(QuestionAppearance.created_at.desc()).all()
    return jsonify({
        "question_id": question_id,
        "qid": getattr(q, 'qid', None),
        "items": [a.to_dict() for a in apps],
        "total": len(apps),
    })


@imports_bp.post("/knowledge/reindex")
@roles_required("admin")
def reindex_knowledge_search():
    """
    Placeholder for future vector reindex - currently just returns stats
    """
    total_q = Question.query.count()
    total_appearances = QuestionAppearance.query.count() if QuestionAppearance else 0
    needs_review = 0
    try:
        needs_review = Question.query.filter_by(needs_review=True).count()
    except Exception:
        pass
    return jsonify({
        "message": "Knowledge Bank stats",
        "total_questions": total_q,
        "total_appearances": total_appearances,
        "needs_review": needs_review,
        "engine_version": knowledge_engine.version if knowledge_engine else "unavailable",
    })

