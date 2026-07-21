"""Exam CRUD and question assignment routes under ``/api/exams``."""

from __future__ import annotations

import logging
from typing import Any, Dict, Final, List, Optional, Set

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required

from app.extensions import db
from app.models.exam import EXAM_MODES, EXAM_STATUSES, Exam, ExamQuestion, ExamSection
from app.models.question import Question
from app.services.rule_engine import ExamRuleEngine, apply_column_sync_to_rules, merge_exam_rules
from app.utils.decorators import roles_required
from app.utils.validators import parse_pagination, require_fields

exams_bp = Blueprint("exams", __name__)
logger = logging.getLogger("exam_os.routes.exams")

_MAX_TITLE: Final[int] = 255
_MAX_TEXT: Final[int] = 50_000
_MAX_SEARCH: Final[int] = 200
_MAX_ASSIGN: Final[int] = 500
_MAX_SECTIONS_ON_CREATE: Final[int] = 50
_MIN_DURATION: Final[int] = 1
_MAX_DURATION: Final[int] = 24 * 3600 * 7  # 7 days hard cap


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


def _clip(value: Any, max_len: int) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:max_len]


def _is_admin() -> bool:
    try:
        return get_jwt().get("role") == "admin"
    except Exception:  # noqa: BLE001
        return False


def _strip_answers_from_exam_dict(data: Dict[str, Any]) -> None:
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        for eq in sec.get("questions") or []:
            if not isinstance(eq, dict):
                continue
            q = eq.get("question")
            if isinstance(q, dict):
                q.pop("correct_answer", None)
                q.pop("explanation", None)
                q.pop("explanation_html", None)


@exams_bp.get("")
@jwt_required()
def list_exams():
    page, per_page = parse_pagination(request.args)
    q = Exam.query
    claims = get_jwt()
    # Students only see published
    if claims.get("role") != "admin":
        q = q.filter_by(status="published")
    elif request.args.get("status"):
        status = request.args.get("status")
        if status not in EXAM_STATUSES:
            return jsonify({"error": "Invalid status"}), 400
        q = q.filter_by(status=status)
    if request.args.get("exam_mode"):
        mode = request.args.get("exam_mode")
        if mode not in EXAM_MODES:
            return jsonify({"error": "Invalid exam_mode"}), 400
        q = q.filter_by(exam_mode=mode)
    if request.args.get("parent_id") is not None:
        parent_id_raw = request.args.get("parent_id") or ""
        if parent_id_raw.strip().lower() in ("null", "none", "nil", ""):
            q = q.filter(Exam.parent_exam_id.is_(None))
        else:
            parent_id = _safe_int(parent_id_raw)
            if parent_id is None:
                return jsonify({"error": "Invalid parent_id"}), 400
            q = q.filter_by(parent_exam_id=parent_id)
    search = (request.args.get("search") or "").strip()[:_MAX_SEARCH]
    if search:
        q = q.filter(Exam.title.ilike(f"%{_escape_like(search)}%", escape="\\"))

    total = q.count()
    items = (
        q.order_by(Exam.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return jsonify({
        "items": [e.to_dict(include_sections=True) for e in items],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@exams_bp.get("/<int:exam_id>")
@jwt_required()
def get_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    claims = get_jwt()
    if claims.get("role") != "admin" and exam.status != "published":
        return jsonify({"error": "Exam not available"}), 403
    include_q = request.args.get("include_questions", "false").lower() == "true"
    # Never leak answers on public get
    data = exam.to_dict(include_sections=True, include_questions=include_q)
    if exam.parent_exam_id is None:
        children_q = Exam.query.filter_by(parent_exam_id=exam.id)
        if claims.get("role") != "admin":
            children_q = children_q.filter_by(status="published")
        children = children_q.order_by(Exam.id.desc()).all()
        if children:
            data["children"] = [c.to_dict(include_sections=True) for c in children]
    if include_q and claims.get("role") != "admin":
        _strip_answers_from_exam_dict(data)
    # Additive: resolved rule pack for clients (backward compatible)
    try:
        data["resolved_rules"] = ExamRuleEngine.from_exam(exam).to_public_dict()
    except Exception:
        data["resolved_rules"] = {}
    return jsonify(data)


@exams_bp.post("")
@roles_required("admin")
def create_exam():
    data = _json_body()
    err = require_fields(data, ["title"])
    if err:
        return jsonify({"error": err}), 400

    title = str(data["title"]).strip()[:_MAX_TITLE]
    if not title:
        return jsonify({"error": "title is required"}), 400

    exam_mode = data.get("exam_mode", "mock")
    if exam_mode not in EXAM_MODES:
        exam_mode = "mock"
    status = data.get("status", "draft")
    if status not in EXAM_STATUSES:
        status = "draft"

    parent_exam_id = data.get("parent_exam_id")
    if parent_exam_id not in (None, ""):
        parent_exam_id = _safe_int(parent_exam_id)
        if parent_exam_id is None:
            return jsonify({"error": "Invalid parent_exam_id"}), 400
        if not Exam.query.get(parent_exam_id):
            return jsonify({"error": "parent_exam_id references unknown exam"}), 400
    else:
        parent_exam_id = None

    duration = _safe_int(data.get("duration_seconds", 3600), 3600) or 3600
    duration = max(_MIN_DURATION, min(duration, _MAX_DURATION))

    max_tabs = _safe_int(data.get("max_tab_switches", 5), 5)
    if max_tabs is None or max_tabs < 0:
        max_tabs = 5
    max_tabs = min(max_tabs, 10_000)

    try:
        created_by = int(get_jwt_identity())
    except (TypeError, ValueError):
        created_by = None

    exam = Exam(
        title=title,
        description=_clip(data.get("description"), _MAX_TEXT),
        instructions=_clip(data.get("instructions"), _MAX_TEXT),
        exam_mode=exam_mode,
        status=status,
        parent_exam_id=parent_exam_id,
        duration_seconds=duration,
        strict_sections=bool(data.get("strict_sections", False)),
        default_marks=_safe_float(data.get("default_marks", 1.0), 1.0),
        default_negative_marks=_safe_float(data.get("default_negative_marks", 0.25), 0.25),
        shuffle_questions=bool(data.get("shuffle_questions", False)),
        shuffle_options=bool(data.get("shuffle_options", False)),
        require_fullscreen=bool(data.get("require_fullscreen", False)),
        max_tab_switches=max_tabs,
        show_result_immediately=bool(data.get("show_result_immediately", True)),
        created_by=created_by,
    )
    # Configuration-driven rules (merged with defaults + classic columns)
    raw_rules = data.get("rules") if isinstance(data.get("rules"), dict) else {}
    exam.set_rules(apply_column_sync_to_rules(exam, raw_rules))

    db.session.add(exam)
    db.session.flush()

    sections = data.get("sections") or []
    if isinstance(sections, list):
        for i, sec in enumerate(sections[:_MAX_SECTIONS_ON_CREATE]):
            if not isinstance(sec, dict):
                continue
            section = ExamSection(
                exam_id=exam.id,
                title=_clip(sec.get("title", f"Section {i + 1}"), 200) or f"Section {i + 1}",
                description=_clip(sec.get("description"), _MAX_TEXT),
                order_index=_safe_int(sec.get("order_index", i), i) or i,
                duration_seconds=_safe_int(sec.get("duration_seconds")),
                subject_id=_safe_int(sec.get("subject_id")),
            )
            db.session.add(section)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_exam failed")
        return jsonify({"error": "Could not create exam"}), 500
    # AUTO-GENERATE child tests from the file bank for this exam's syllabus.
    # Only topics that actually have questions in files get a test (skip_silent).
    auto_summary = None
    try:
        if bool(data.get("auto_generate", True)):
            from app.services.auto_test import generate_tests_for_exam
            auto_summary = generate_tests_for_exam(exam)
    except Exception:
        logger.exception("auto test generation failed for exam=%s", exam.id)

    resp = {"message": "Exam created", "item": exam.to_dict()}
    if auto_summary is not None:
        resp["auto_tests"] = auto_summary
    return jsonify(resp), 201


@exams_bp.put("/<int:exam_id>")
@roles_required("admin")
def update_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    data = _json_body()

    if "title" in data and data["title"] is not None:
        title = str(data["title"]).strip()[:_MAX_TITLE]
        if title:
            exam.title = title
    if "description" in data:
        exam.description = _clip(data["description"], _MAX_TEXT)
    if "instructions" in data:
        exam.instructions = _clip(data["instructions"], _MAX_TEXT)
    if "exam_mode" in data and data["exam_mode"] in EXAM_MODES:
        exam.exam_mode = data["exam_mode"]
    if "status" in data and data["status"] in EXAM_STATUSES:
        exam.status = data["status"]

    if "duration_seconds" in data and data["duration_seconds"] is not None:
        duration = _safe_int(data["duration_seconds"])
        if duration is not None:
            exam.duration_seconds = max(_MIN_DURATION, min(duration, _MAX_DURATION))
    if "parent_exam_id" in data:
        parent_exam_id = data.get("parent_exam_id")
        if parent_exam_id in (None, ""):
            exam.parent_exam_id = None
        else:
            parent_exam_id = _safe_int(parent_exam_id)
            if parent_exam_id is None:
                return jsonify({"error": "Invalid parent_exam_id"}), 400
            if parent_exam_id == exam.id:
                return jsonify({"error": "Exam cannot be its own parent"}), 400
            if not Exam.query.get(parent_exam_id):
                return jsonify({"error": "parent_exam_id references unknown exam"}), 400
            exam.parent_exam_id = parent_exam_id
    if "max_tab_switches" in data and data["max_tab_switches"] is not None:
        tabs = _safe_int(data["max_tab_switches"])
        if tabs is not None and tabs >= 0:
            exam.max_tab_switches = min(tabs, 10_000)

    if "default_marks" in data and data["default_marks"] is not None:
        exam.default_marks = _safe_float(data["default_marks"], exam.default_marks or 1.0)
    if "default_negative_marks" in data and data["default_negative_marks"] is not None:
        exam.default_negative_marks = _safe_float(
            data["default_negative_marks"], exam.default_negative_marks or 0.0
        )

    for field in (
        "strict_sections", "shuffle_questions", "shuffle_options",
        "require_fullscreen", "show_result_immediately",
    ):
        if field in data:
            setattr(exam, field, bool(data[field]))

    if "rules" in data:
        if data["rules"] is None:
            exam.set_rules(apply_column_sync_to_rules(exam, {}))
        elif isinstance(data["rules"], dict):
            exam.set_rules(apply_column_sync_to_rules(exam, data["rules"]))
    else:
        # Keep rules_json aligned when classic columns change
        exam.set_rules(apply_column_sync_to_rules(exam, exam.get_rules()))

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("update_exam id=%s failed", exam_id)
        return jsonify({"error": "Could not update exam"}), 500
    return jsonify({"message": "Exam updated", "item": exam.to_dict()})


@exams_bp.delete("/<int:exam_id>")
@roles_required("admin")
def delete_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    db.session.delete(exam)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_exam id=%s failed", exam_id)
        return jsonify({"error": "Could not delete exam"}), 500
    return jsonify({"message": "Exam deleted"})


@exams_bp.post("/<int:exam_id>/sections")
@roles_required("admin")
def add_section(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    data = _json_body()
    err = require_fields(data, ["title"])
    if err:
        return jsonify({"error": err}), 400
    title = str(data["title"]).strip()[:200]
    if not title:
        return jsonify({"error": "title is required"}), 400
    order = _safe_int(data.get("order_index"), len(exam.sections))
    if order is None:
        order = len(exam.sections)
    sec = ExamSection(
        exam_id=exam.id,
        title=title,
        description=_clip(data.get("description"), _MAX_TEXT),
        order_index=order,
        duration_seconds=_safe_int(data.get("duration_seconds")),
        subject_id=_safe_int(data.get("subject_id")),
    )
    db.session.add(sec)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("add_section exam_id=%s failed", exam_id)
        return jsonify({"error": "Could not add section"}), 500
    return jsonify({"message": "Section added", "item": sec.to_dict()}), 201


@exams_bp.put("/<int:exam_id>/sections/<int:section_id>")
@roles_required("admin")
def update_section(exam_id, section_id):
    sec = ExamSection.query.filter_by(id=section_id, exam_id=exam_id).first_or_404()
    data = _json_body()
    if "title" in data:
        title = str(data["title"]).strip()[:200]
        if title:
            sec.title = title
    if "description" in data:
        sec.description = _clip(data["description"], _MAX_TEXT)
    if "order_index" in data:
        oi = _safe_int(data["order_index"])
        if oi is not None:
            sec.order_index = oi
    if "duration_seconds" in data:
        sec.duration_seconds = _safe_int(data["duration_seconds"])
    if "subject_id" in data:
        sec.subject_id = _safe_int(data["subject_id"])
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("update_section %s/%s failed", exam_id, section_id)
        return jsonify({"error": "Could not update section"}), 500
    return jsonify({"message": "Section updated", "item": sec.to_dict()})


@exams_bp.delete("/<int:exam_id>/sections/<int:section_id>")
@roles_required("admin")
def delete_section(exam_id, section_id):
    sec = ExamSection.query.filter_by(id=section_id, exam_id=exam_id).first_or_404()
    db.session.delete(sec)
    exam = Exam.query.get(exam_id)
    if exam:
        exam.recalculate_totals()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_section %s/%s failed", exam_id, section_id)
        return jsonify({"error": "Could not delete section"}), 500
    return jsonify({"message": "Section deleted"})


@exams_bp.post("/<int:exam_id>/questions")
@roles_required("admin")
def assign_questions(exam_id):
    """
    Body: { question_ids: [1,2,3], section_id?: int, marks?: float, negative_marks?: float }
    """
    exam = Exam.query.get_or_404(exam_id)
    data = _json_body()
    qids = data.get("question_ids") or []
    if not isinstance(qids, list) or not qids:
        return jsonify({"error": "question_ids required"}), 400
    if len(qids) > _MAX_ASSIGN:
        return jsonify({"error": f"At most {_MAX_ASSIGN} question_ids per request"}), 400

    section_id = data.get("section_id")
    if section_id not in (None, ""):
        section_id = _safe_int(section_id)
        if section_id is None:
            return jsonify({"error": "Invalid section_id"}), 400
        sec = ExamSection.query.filter_by(id=section_id, exam_id=exam.id).first()
        if not sec:
            return jsonify({"error": "Section not found in this exam"}), 404
    else:
        section_id = None

    existing: Set[int] = {eq.question_id for eq in exam.exam_questions.all()}
    start_order = exam.exam_questions.count()
    added: List[int] = []
    for i, raw_qid in enumerate(qids):
        qid = _safe_int(raw_qid)
        if qid is None or qid in existing:
            continue
        q = Question.query.get(qid)
        if not q:
            continue
        marks_default = q.marks if q.marks is not None else exam.default_marks
        neg_default = (
            q.negative_marks if q.negative_marks is not None else exam.default_negative_marks
        )
        eq = ExamQuestion(
            exam_id=exam.id,
            section_id=section_id,
            question_id=qid,
            order_index=start_order + i,
            marks=_safe_float(data.get("marks", marks_default), float(marks_default or 1)),
            negative_marks=_safe_float(
                data.get("negative_marks", neg_default), float(neg_default or 0)
            ),
        )
        db.session.add(eq)
        added.append(qid)
        existing.add(qid)

    db.session.flush()
    exam.recalculate_totals()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("assign_questions exam_id=%s failed", exam_id)
        return jsonify({"error": "Could not assign questions"}), 500
    return jsonify({
        "message": f"Assigned {len(added)} questions",
        "added": added,
        "item": exam.to_dict(include_sections=True, include_questions=True),
    })


@exams_bp.delete("/<int:exam_id>/questions/<int:exam_question_id>")
@roles_required("admin")
def remove_question(exam_id, exam_question_id):
    eq = ExamQuestion.query.filter_by(id=exam_question_id, exam_id=exam_id).first_or_404()
    db.session.delete(eq)
    exam = Exam.query.get(exam_id)
    db.session.flush()
    if exam:
        exam.recalculate_totals()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("remove_question %s/%s failed", exam_id, exam_question_id)
        return jsonify({"error": "Could not remove question"}), 500
    return jsonify({"message": "Question removed from exam"})


@exams_bp.post("/<int:exam_id>/publish")
@roles_required("admin")
def publish_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    # Recalculate to avoid stale denormalized zero
    exam.recalculate_totals()
    if exam.total_questions < 1:
        return jsonify({"error": "Cannot publish exam with no questions"}), 400
    exam.status = "published"
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("publish_exam id=%s failed", exam_id)
        return jsonify({"error": "Could not publish exam"}), 500
    return jsonify({"message": "Exam published", "item": exam.to_dict()})


@exams_bp.post("/<int:exam_id>/unpublish")
@roles_required("admin")
def unpublish_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    exam.status = "draft"
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("unpublish_exam id=%s failed", exam_id)
        return jsonify({"error": "Could not unpublish exam"}), 500
    return jsonify({"message": "Exam unpublished", "item": exam.to_dict()})


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Schedule window enforcement (starts_at/ends_at) on start_attempt
# - Optimistic locking when publishing concurrent edits
# --------------------------------------------

# FILE-BASED REAL TEST SYSTEM - Chapter wise, Topic wise, Subject wise, Full Mock
# Put file in questions_data folder, create test like real SSC exam

@exams_bp.get("/file-bank/stats")
@jwt_required()
def file_bank_stats():
    try:
        from app.services import file_bank as _fb
        from app.services.file_bank import get_stats
        stats = get_stats()
        return jsonify({
            "message": "File bank stats - real test jaisa",
            "total_file_questions": len(_fb.FILE_QUESTIONS),
            "stats": stats,
            "available_test_types": ["chapter_wise", "topic_wise", "subject_wise", "full_mock", "pyq", "difficulty_wise", "random"],
            "example": {
                "chapter_wise": "Analogy chapter ke 20 questions ka test",
                "topic_wise": "Number Analogy topic ke 15 questions",
                "subject_wise": "Reasoning subject ke 50 questions",
                "full_mock": "Full 100 questions mock like real SSC"
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

@exams_bp.post("/<int:exam_id>/make-tests-by-ai")
@roles_required("admin")
def make_tests_by_ai(exam_id):
    """
    Manual trigger: build syllabus tests for an exam whose topics weren't found
    in the file bank at creation time. AI is allowed to fill missing answers.
    """
    exam = Exam.query.get_or_404(exam_id)
    try:
        from app.services.auto_test import generate_tests_for_exam
        summary = generate_tests_for_exam(exam)
        if summary.get("created", 0) == 0:
            return jsonify({
                "message": "Koi naya test nahi bana - in topics ke questions file bank me nahi mile.",
                "hint": "Us topic ki .txt file questions_data me daalo, ya AI key set karo.",
                **summary,
            })
        return jsonify({"message": f"{summary['created']} tests ban gaye", **summary})
    except Exception as e:
        logger.exception("make_tests_by_ai failed exam=%s", exam_id)
        return jsonify({"error": str(e)[:300]}), 500


@exams_bp.post("/file-bank/cleanup-auto")
@roles_required("admin")
def cleanup_auto_tests():
    """
    Delete previously auto-generated tests that were wrongly created as
    top-level (standalone) exams. Keeps manual exams and subject containers.
    Use this once, then run reload to regenerate them correctly (inside a
    subject container).
    """
    try:
        removed = []
        for ex in Exam.query.filter_by(parent_exam_id=None).all():
            try:
                rules = ex.get_rules() or {}
            except Exception:
                rules = {}
            is_container = bool(rules.get("auto_container"))
            # A file-bank test has either an auto_generated tag OR a
            # file_bank_source config (older tests). Remove those top-level ones
            # so they can be regenerated inside a subject container. Keep
            # containers and genuinely-manual exams.
            is_file_bank_test = bool(rules.get("auto_generated")) or bool(rules.get("file_bank_source"))
            if is_file_bank_test and not is_container:
                removed.append({"exam_id": ex.id, "title": ex.title})
                db.session.delete(ex)
        db.session.commit()
        return jsonify({
            "message": f"{len(removed)} galat top-level auto-tests hataye. Ab 'Reload & Auto-Generate' dabao — sahi jagah (exam ke andar) ban jayenge.",
            "removed": removed,
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)[:300]}), 500


@exams_bp.post("/file-bank/reload")
@roles_required("admin")
def file_bank_reload():
    """Re-scan the questions_data folder after adding/updating .txt files."""
    try:
        from app.services.file_bank import reload_file_bank, get_stats
        n = reload_file_bank()
        auto_summary = None
        # After new files load, auto-build standalone tests for the new content.
        if request.args.get("auto", "true").lower() != "false":
            try:
                from app.services.auto_test import generate_tests_for_bank
                auto_summary = generate_tests_for_bank()
            except Exception:
                logger.exception("auto test generation after reload failed")
        return jsonify({
            "message": f"File bank reloaded - {n} questions",
            "total": n,
            "stats": get_stats(),
            "auto_tests": auto_summary,
        })
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

@exams_bp.get("/file-bank/questions")
@jwt_required()
def file_bank_questions():
    try:
        from app.services.file_bank import FILE_QUESTIONS
        # Query params: subject, chapter, topic, difficulty, count
        subject = request.args.get("subject")
        chapter = request.args.get("chapter")
        topic = request.args.get("topic")
        difficulty = request.args.get("difficulty")
        count = int(request.args.get("count", 20))
        count = min(count, 100)  # Max 100 per preview
        
        from app.services.file_bank import filter_questions
        filtered = filter_questions(subject=subject, chapter=chapter, topic=topic, difficulty=difficulty, count=count)
        
        return jsonify({
            "total_in_file_bank": len(FILE_QUESTIONS),
            "filtered_count": len(filtered),
            "filters": {"subject": subject, "chapter": chapter, "topic": topic, "difficulty": difficulty, "count": count},
            "questions": filtered[:count]
        })
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500

@exams_bp.post("/import-from-files")
@roles_required("admin")
def import_from_files():
    """
    Real test jaisa - Chapter wise, Topic wise, Subject wise, Full Mock
    Body: {
      "test_type": "chapter_wise" | "topic_wise" | "subject_wise" | "full_mock" | "random",
      "subject": "Reasoning",
      "chapter": "Analogy",
      "topic": "Number Analogy",  # optional - Number Analogy, Word Analogy, etc
      "difficulty": "easy" | "medium" | "hard" | null,
      "count": 20,
      "title": "Custom Test Name"
    }
    """
    try:
        from app.services.file_bank import FILE_QUESTIONS, filter_questions, get_stats
        
        data = request.get_json(silent=True) or {}
        
        if not FILE_QUESTIONS:
            return jsonify({"error": "questions_data folder me koi txt file nahi mili. Folder: backend/questions_data/ me .txt file daalo"}), 404
        
        test_type = data.get("test_type", "chapter_wise")
        subject = data.get("subject")
        chapter = data.get("chapter")
        topic = data.get("topic")  # e.g., Number Analogy, Word Analogy, SI Units
        difficulty = data.get("difficulty")
        title = data.get("title")

        # ---- Real-paper question count (how many each attempt SHOWS) ----
        # This is the number a student sees per attempt, like a real paper.
        DEFAULT_COUNTS = {
            "topic_wise": 12,       # topic test: 10-15 questions
            "chapter_wise": 20,     # chapter test: ~20-25
            "subject_wise": 25,     # subject test
            "full_mock": 100,       # full mock like real SSC
            "difficulty_wise": 20,
            "random": 20,
        }
        if data.get("count") in (None, "", 0):
            per_attempt = DEFAULT_COUNTS.get(test_type, 20)
        else:
            per_attempt = int(data.get("count"))
        if test_type == "full_mock":
            per_attempt = max(10, min(per_attempt, 200))
        elif test_type == "topic_wise":
            per_attempt = max(5, min(per_attempt, 15))
        else:
            per_attempt = max(5, min(per_attempt, 100))

        # ---- Pool size (how many questions we STORE once, shared by all users) ----
        # We store a big pool so returning users can get FRESH questions without
        # any rebuild / AI calls. Pool is capped to keep the DB sane.
        _MAX_POOL = 500
        pool_size = min(_MAX_POOL, max(per_attempt, int(data.get("pool_size") or per_attempt * 6)))
        # This is the number used for FILTERING the file bank below.
        count = pool_size
        
        # Filter questions based on real test logic
        if test_type == "chapter_wise":
            filtered = filter_questions(chapter=chapter, difficulty=difficulty, count=count)
            label = chapter or "Chapter"
            if not title:
                title = f"{label} - Chapter Test - {len(filtered)} Qs"
            desc = f"Chapter wise test: {label} - {len(filtered)} questions from file bank"
        
        elif test_type == "topic_wise":
            filtered = filter_questions(topic=topic, difficulty=difficulty, count=count)
            label = topic or "Topic"
            if not title:
                title = f"{label} - Topic Test - {len(filtered)} Qs"
            desc = f"Topic wise test: {label} - focused practice"
        
        elif test_type == "subject_wise":
            filtered = filter_questions(subject=subject, difficulty=difficulty, count=count)
            label = subject or "Subject"
            if not title:
                title = f"{label} - Subject Test - {len(filtered)} Qs"
            desc = f"Subject wise test: {label}"
        
        elif test_type == "full_mock":
            # Full mock: mix of all topics, 100 questions like real SSC
            filtered = filter_questions(count=count)
            if not title:
                title = f"Full Mock Test - {len(filtered)} Qs - Real Exam Pattern"
            desc = f"Full mock like real SSC - {len(filtered)} questions mixed"
        
        elif test_type == "difficulty_wise":
            filtered = filter_questions(difficulty=difficulty or "medium", count=count)
            if not title:
                title = f"{(difficulty or 'medium').title()} Level Test - {len(filtered)} Qs"
            desc = f"Difficulty wise: {difficulty}"
        
        else:  # random
            filtered = filter_questions(subject=subject, chapter=chapter, topic=topic, difficulty=difficulty, count=count)
            if not title:
                title = f"Practice Test - {len(filtered)} Qs"
            desc = f"Practice test from file bank - {len(filtered)} Qs"
        
        if not filtered:
            stats = get_stats()
            return jsonify({
                "error": f"No questions found for filters",
                "filters": {"test_type": test_type, "subject": subject, "chapter": chapter, "topic": topic, "difficulty": difficulty},
                "available": stats,
                "hint": "Try topic='Number Analogy' or chapter='Analogy' or no filters for random"
            }), 404
        
        # Create real exam like SSC pattern.
        # Duration is based on what a student SEES per attempt (per_attempt),
        # NOT the whole stored pool.
        exam = Exam(
            title=title[:255],
            description=desc[:1000],
            duration_seconds=per_attempt * 60,  # 1 min per shown question
            status="published",
            exam_mode="mock",
            default_marks=2,
            default_negative_marks=0.5
        )
        db.session.add(exam)
        db.session.flush()
        
        # Create section
        section = ExamSection(
            exam_id=exam.id,
            title=chapter or topic or subject or "General",
            order_index=0
        )
        db.session.add(section)
        db.session.flush()
        
        # Optional AI fallback for questions with no answer in the file
        try:
            from app.services.knowledge_engine.free_ai_chain import derive_answer_with_ai
            AI_ANSWER_AVAILABLE = True
        except Exception:
            AI_ANSWER_AVAILABLE = False
        ai_has_keys = any([
            __import__("os").getenv(k) for k in
            ("GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
        ])

        # Add questions to DB and exam
        added = 0
        ai_derived = 0
        skipped_no_answer = 0
        for idx, fq in enumerate(filtered):
            try:
                options = fq.get("options", [])[:4]

                # --- Resolve correct answer: FILE FIRST, then AI ---
                correct = fq.get("correct_answer")
                explanation = fq.get("explanation") or ""
                answer_source = fq.get("answer_source", "file")

                if not correct and AI_ANSWER_AVAILABLE and ai_has_keys:
                    ai = derive_answer_with_ai(fq["question_text"], options)
                    if ai:
                        correct = ai["correct_answer"]
                        explanation = explanation or ai.get("explanation", "")
                        answer_source = ai.get("source", "ai")
                        ai_derived += 1

                # Still no answer -> skip (never guess "A")
                if not correct:
                    skipped_no_answer += 1
                    continue
                # Ensure the chosen key actually exists among options
                valid_keys = {str(o.get("option_key", "")).upper() for o in options}
                if correct not in valid_keys:
                    skipped_no_answer += 1
                    continue

                q = Question(
                    question_text=fq["question_text"][:2000],
                    question_type="single_choice",
                    difficulty=fq.get("difficulty","medium") if fq.get("difficulty") in ["easy","medium","hard"] else "medium",
                    correct_answer=correct,
                    explanation=explanation[:2000] if explanation else None,
                    marks=2,
                    negative_marks=0.5,
                    is_active=True,
                    tags=f"{fq.get('subject','')},{fq.get('chapter','')},{fq.get('topic','')},{fq.get('pattern','')},src:{answer_source}"[:512],
                    source=fq.get("source","file_bank")
                )
                db.session.add(q)
                db.session.flush()
                
                from app.models.question import QuestionOption
                for opt_idx, opt in enumerate(options):
                    db.session.add(QuestionOption(
                        question_id=q.id,
                        option_key=opt.get("option_key","A"),
                        option_text=opt.get("option_text","")[:500],
                        order_index=opt_idx
                    ))
                
                db.session.add(ExamQuestion(
                    exam_id=exam.id,
                    section_id=section.id,
                    question_id=q.id,
                    order_index=added,
                    marks=2,
                    negative_marks=0.5
                ))
                added += 1
                
                # Commit every 20 to avoid memory
                if added % 20 == 0:
                    try:
                        db.session.commit()
                        db.session.begin()
                    except Exception:
                        db.session.rollback()
                        db.session.begin()
                        
            except Exception as e:
                logger.warning(f"Failed to add file question {idx}: {e}")
                continue
        
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "Failed to save exam"}), 500

        # Keep exam totals accurate and remember the pool config so each attempt
        # can draw a real-paper-sized subset (with per-user no-repeat).
        shown = min(per_attempt, added)
        try:
            exam.recalculate_totals()
            exam.duration_seconds = max(60, shown * 60)  # ~1 min per shown question
            rules = exam.get_rules() if hasattr(exam, "get_rules") else {}
            if not isinstance(rules, dict):
                rules = {}
            rules["file_bank_source"] = {
                "test_type": test_type,
                "subject": subject,
                "chapter": chapter,
                "topic": topic,
                "difficulty": difficulty,
                "no_repeat_correct": True,
                "questions_per_attempt": shown,   # what a student sees each attempt
                "pool_size": added,               # total stored (shared by all users)
            }
            exam.set_rules(rules)
            db.session.commit()
        except Exception:
            db.session.rollback()

        if added == 0:
            return jsonify({
                "error": "Koi valid answer wale questions nahi mile is filter me.",
                "hint": "File me answer key honi chahiye, ya AI key (GEMINI_API_KEY/GROQ_API_KEY) set karo taki AI answer nikaale.",
                "skipped_no_answer": skipped_no_answer,
                "filters_used": {"test_type": test_type, "subject": subject, "chapter": chapter, "topic": topic, "difficulty": difficulty},
            }), 404

        return jsonify({
            "message": f"Real test jaisa ban gaya! {added} questions - {test_type}",
            "exam_id": exam.id,
            "exam": exam.to_dict(),
            "test_type": test_type,
            "filters_used": {"subject": subject, "chapter": chapter, "topic": topic, "difficulty": difficulty, "count": count},
            "questions_added": added,
            "answers_from_file": added - ai_derived,
            "answers_from_ai": ai_derived,
            "skipped_no_answer": skipped_no_answer,
            "stats": get_stats()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.exception("import_from_files failed")
        return jsonify({"error": str(e)[:800]}), 500
