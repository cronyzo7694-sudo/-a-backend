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
    def _item(e):
        # List view only needs summary fields — NOT sections/questions.
        # Loading sections here caused huge JOINs (sections->exam_questions->
        # questions->options) and made the list slow under load.
        d = e.to_dict(include_sections=False)
        try:
            cs = (e.get_rules() or {}).get("coming_soon") or {}
            d["coming_soon"] = bool(cs.get("active"))
        except Exception:
            d["coming_soon"] = False
        return d

    return jsonify({
        "items": [_item(e) for e in items],
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
            # Child cards need summary only; skip sections for speed.
            child_list = []
            for c in children:
                cd = c.to_dict(include_sections=False)
                try:
                    ccs = (c.get_rules() or {}).get("coming_soon") or {}
                    cd["coming_soon"] = bool(ccs.get("active"))
                except Exception:
                    cd["coming_soon"] = False
                child_list.append(cd)
            data["children"] = child_list
    if include_q and claims.get("role") != "admin":
        _strip_answers_from_exam_dict(data)
    # Additive: resolved rule pack for clients (backward compatible)
    try:
        data["resolved_rules"] = ExamRuleEngine.from_exam(exam).to_public_dict()
    except Exception:
        data["resolved_rules"] = {}
    # Additive: coming-soon flag (exam has no tests yet, questions not in files)
    try:
        cs = (exam.get_rules() or {}).get("coming_soon") or {}
        data["coming_soon"] = bool(cs.get("active"))
        data["coming_soon_reason"] = cs.get("reason", "")
    except Exception:
        data["coming_soon"] = False
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
    try:
        from sqlalchemy import text

        # Gather this exam + its child exams (pool tests live as children).
        exam_ids = [exam.id]
        try:
            for child in Exam.query.filter_by(parent_exam_id=exam.id).all():
                exam_ids.append(child.id)
        except Exception:
            pass

        # Delete bottom-up with RAW SQL to bypass SQLAlchemy's ORM cascade,
        # which otherwise tries to UPDATE attempts.exam_id = NULL (the crash).
        # Order: attempt_answers -> attempts -> exam_questions -> sections -> exams
        # Uses expanding IN bindparams so it works on BOTH Postgres and SQLite.
        from sqlalchemy import bindparam
        conn = db.session.connection()

        def _run(sql, ids):
            conn.execute(text(sql).bindparams(bindparam("ids", expanding=True)), {"ids": ids})

        _run("DELETE FROM attempt_answers WHERE attempt_id IN "
             "(SELECT id FROM attempts WHERE exam_id IN :ids)", exam_ids)
        _run("DELETE FROM attempt_answers WHERE exam_question_id IN "
             "(SELECT id FROM exam_questions WHERE exam_id IN :ids)", exam_ids)
        _run("DELETE FROM attempts WHERE exam_id IN :ids", exam_ids)
        _run("DELETE FROM exam_questions WHERE exam_id IN :ids", exam_ids)
        _run("DELETE FROM exam_sections WHERE exam_id IN :ids", exam_ids)
        # Child exams first, then the parent.
        child_ids = [i for i in exam_ids if i != exam.id]
        if child_ids:
            _run("DELETE FROM exams WHERE id IN :ids", child_ids)
        conn.execute(text("DELETE FROM exams WHERE id = :id"), {"id": exam.id})

        # Expire ORM identity map so stale objects aren't re-flushed.
        db.session.expire_all()
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


# ============================================================================
# FILE BANK (read-only) + EXAM-DRIVEN AUTO TESTS
# Design: NO exam -> NO test. Exam with no matching questions -> "Coming Soon".
# Questions come from questions_data/*.txt; tests live INSIDE the exam.
# ============================================================================

@exams_bp.get("/file-bank/stats")
@jwt_required()
def file_bank_stats():
    try:
        from app.services import file_bank as _fb
        from app.services.file_bank import get_stats
        return jsonify({
            "message": "File bank stats",
            "total_file_questions": len(_fb.FILE_QUESTIONS),
            "stats": get_stats(),
        })
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@exams_bp.post("/file-bank/reload")
@roles_required("admin")
def file_bank_reload():
    """Re-scan questions_data, then re-check EVERY exam so new questions turn
    'Coming Soon' exams into real tests automatically."""
    try:
        from app.services.file_bank import reload_file_bank, get_stats
        from app.services.auto_test import refresh_all_exams
        n = reload_file_bank()
        refreshed = refresh_all_exams()
        return jsonify({
            "message": f"File bank reloaded - {n} questions. Exams re-checked.",
            "total": n,
            "stats": get_stats(),
            "refresh": refreshed,
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)[:300]}), 500


@exams_bp.post("/<int:exam_id>/generate-tests")
@roles_required("admin")
def generate_tests(exam_id):
    """Manually re-run auto test generation for one exam (rarely needed;
    creation + file reload already do this automatically)."""
    exam = Exam.query.get_or_404(exam_id)
    try:
        from app.services.auto_test import generate_tests_for_exam
        summary = generate_tests_for_exam(exam)
        return jsonify({"message": "Tests refreshed", **summary})
    except Exception as e:
        db.session.rollback()
        logger.exception("generate_tests failed exam=%s", exam_id)
        return jsonify({"error": str(e)[:300]}), 500


@exams_bp.post("/admin/factory-reset")
@roles_required("admin")
def factory_reset():
    """DANGER: delete ALL exams, tests, attempts and their questions/answers so
    you can start fresh. Users, subjects and file bank are kept. Also purges any
    broken (exam_id NULL / orphan) attempt rows. Uses raw SQL to avoid ORM
    cascade issues on PostgreSQL."""
    try:
        from sqlalchemy import text
        conn = db.session.connection()
        # Order matters (children before parents); raw SQL bypasses ORM cascade.
        conn.execute(text("DELETE FROM attempt_answers"))
        conn.execute(text("DELETE FROM attempts"))
        conn.execute(text("DELETE FROM exam_questions"))
        conn.execute(text("DELETE FROM exam_sections"))
        # questions created by auto/file tests (source marks them) - safe to drop
        conn.execute(text("DELETE FROM question_options WHERE question_id IN (SELECT id FROM questions WHERE source IS NOT NULL AND source <> '' )"))
        conn.execute(text("DELETE FROM questions WHERE source IS NOT NULL AND source <> ''"))
        conn.execute(text("DELETE FROM exams"))
        db.session.expire_all()
        db.session.commit()
        return jsonify({"message": "Factory reset done. Ab exam banao - test apne aap ban jayenge (agar file me questions hain)."})
    except Exception as e:
        db.session.rollback()
        logger.exception("factory_reset failed")
        return jsonify({"error": str(e)[:300]}), 500
