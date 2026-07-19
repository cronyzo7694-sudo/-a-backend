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

imports_bp = Blueprint("imports", __name__)
logger = logging.getLogger("exam_os.routes.imports")

_MAX_JSON_ROWS: Final[int] = 1000
_MAX_CSV_ROWS: Final[int] = 2000
_MAX_CSV_BYTES: Final[int] = 5 * 1024 * 1024
_MAX_TEXT: Final[int] = 50_000
_MAX_OPTIONS: Final[int] = 10
_MAX_ERRORS_STORED: Final[int] = 200


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
    db.session.add(q)
    db.session.flush()

    if qtype != "integer":
        for i, (key, text_opt) in enumerate(options[:_MAX_OPTIONS]):
            db.session.add(
                QuestionOption(
                    question_id=q.id,
                    option_key=str(key)[:8],
                    option_text=text_opt or "",
                    order_index=i,
                )
            )

    # Tag mapping table
    try:
        for tag in _ensure_tags(tag_list):
            if not QuestionTag.query.filter_by(question_id=q.id, tag_id=tag.id).first():
                db.session.add(QuestionTag(question_id=q.id, tag_id=tag.id))
    except Exception:
        logger.exception("tag mapping failed")

    exam_id = _resolve_exam_id(row, defaults)
    if exam_id:
        _map_to_exam(q, exam_id, {**defaults, **row})

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

    for i, row in enumerate(rows):
        try:
            q, err, status = _create_from_row(
                row if isinstance(row, dict) else {},
                user_id,
                defaults,
                skip_duplicates=skip_dup,
            )
        except Exception:
            logger.exception("import row %s failed", i + 1)
            errors.append({"row": i + 1, "error": "row processing failed"})
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
