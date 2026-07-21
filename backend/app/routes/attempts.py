"""Exam attempt lifecycle: start, save answers, submit, result, review.

Stable routes under ``/api/attempts``. Never leak correct answers while
``status == in_progress``. Owner-or-admin authorization on all attempt ids.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Final, List, Optional

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required

from app.extensions import db
from app.models.attempt import Attempt, AttemptAnswer
from app.models.exam import Exam, ExamQuestion
from app.models.question import Question
from app.models.user import User
from app.services.rule_engine import ExamRuleEngine
from app.services.scoring import evaluate_answer
from app.services.analytics_engine import build_attempt_analytics
from app.services.permission_engine import check_exam_access
from app.utils.validators import parse_pagination

attempts_bp = Blueprint("attempts", __name__)
logger = logging.getLogger("exam_os.routes.attempts")

_MAX_BULK_ANSWERS: Final[int] = 500
_MAX_SECURITY_MSG: Final[int] = 500
_MAX_TIME_SPENT: Final[int] = 7 * 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _identity_int() -> Optional[int]:
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


def _is_admin() -> bool:
    try:
        return get_jwt().get("role") == "admin"
    except Exception:  # noqa: BLE001
        return False


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _authorize_attempt(attempt: Attempt, user_id: int) -> Optional[Any]:
    if attempt.user_id != user_id and not _is_admin():
        return jsonify({"error": "Forbidden"}), 403
    return None


def _build_player_payload(attempt: Attempt, exam: Exam) -> Dict[str, Any]:
    """Assemble exam player payload without leaking correct answers."""
    rules = ExamRuleEngine.from_exam(exam)
    sections_out: List[Dict[str, Any]] = []
    all_eqs = (
        ExamQuestion.query.filter_by(exam_id=exam.id)
        .order_by(ExamQuestion.order_index)
        .all()
    )

    by_section: Dict[Any, List[ExamQuestion]] = {}
    unsectioned: List[ExamQuestion] = []
    for eq in all_eqs:
        if eq.section_id:
            by_section.setdefault(eq.section_id, []).append(eq)
        else:
            unsectioned.append(eq)

    answer_map = {a.question_id: a for a in attempt.answers.all()}
    do_shuffle_opts = rules.shuffle_options()
    do_shuffle_qs = rules.shuffle_questions()
    mandatory = set(rules.mandatory_question_ids())
    optional = set(rules.optional_question_ids())

    def serialize_eq(eq: ExamQuestion, idx: int) -> Dict[str, Any]:
        ans = answer_map.get(eq.question_id)
        qdict = None
        if eq.question is not None:
            qdict = eq.question.to_dict(include_answer=False, include_explanation=False)
            # practice mode may reveal answers after each — only if configured
            if rules.show_answer_after_each() and ans and ans.is_answered:
                qdict = eq.question.to_dict(include_answer=True, include_explanation=True)
        if qdict and do_shuffle_opts and qdict.get("options"):
            opts = list(qdict["options"])
            rng = random.Random(attempt.id * 10007 + eq.question_id)
            rng.shuffle(opts)
            qdict["options"] = opts
        # Per-question marks always from ExamQuestion mapping (not hardcoded)
        marks = float(eq.marks if eq.marks is not None else rules.default_marks())
        neg = float(eq.negative_marks if eq.negative_marks is not None else rules.default_negative_marks())
        if not rules.negative_marking_enabled():
            neg = 0.0
        return {
            "exam_question_id": eq.id,
            "question_id": eq.question_id,
            "order_index": idx,
            "marks": marks,
            "negative_marks": neg,
            "section_id": eq.section_id,
            "is_mandatory": eq.question_id in mandatory,
            "is_optional": eq.question_id in optional,
            "question": qdict,
            "answer": {
                "selected_answer": ans.get_selected_parsed() if ans else None,
                "is_answered": bool(ans.is_answered) if ans else False,
                "is_marked_for_review": bool(ans.is_marked_for_review) if ans else False,
                "is_visited": bool(ans.is_visited) if ans else False,
                "time_spent_seconds": int(ans.time_spent_seconds or 0) if ans else 0,
            },
        }

    global_idx = 0
    try:
        ordered_sections = sorted(exam.sections or [], key=lambda s: s.order_index or 0)
    except Exception:  # noqa: BLE001
        ordered_sections = list(exam.sections or [])

    if ordered_sections:
        for sec in ordered_sections:
            eqs = list(by_section.get(sec.id, []))
            if do_shuffle_qs:
                rng = random.Random(attempt.id * 97 + (sec.id or 0))
                rng.shuffle(eqs)
            items = []
            for eq in eqs:
                items.append(serialize_eq(eq, global_idx))
                global_idx += 1
            sec_duration = sec.duration_seconds
            if rules.section_timers_enabled() and not sec_duration:
                # evenly split overall when section timers on but section blank
                nsec = max(1, len(ordered_sections))
                sec_duration = max(1, rules.overall_seconds() // nsec)
            sections_out.append({
                "id": sec.id,
                "title": sec.title,
                "description": sec.description,
                "order_index": sec.order_index,
                "duration_seconds": sec_duration,
                "questions": items,
            })
    else:
        eqs = list(unsectioned or all_eqs)
        if do_shuffle_qs:
            rng = random.Random(attempt.id * 97)
            rng.shuffle(eqs)
        items = []
        for eq in eqs:
            items.append(serialize_eq(eq, global_idx))
            global_idx += 1
        sections_out.append({
            "id": None,
            "title": "All Questions",
            "description": None,
            "order_index": 0,
            "duration_seconds": None,
            "questions": items,
        })

    remaining = None
    if attempt.expires_at:
        remaining = max(0, int((attempt.expires_at - _utcnow()).total_seconds()))

    public_rules = rules.to_public_dict()
    return {
        "attempt": attempt.to_dict(),
        "exam": {
            "id": exam.id,
            "title": exam.title,
            "instructions": exam.instructions,
            "duration_seconds": rules.overall_seconds(),
            "strict_sections": rules.strict_sections(),
            "require_fullscreen": rules.require_fullscreen(),
            "max_tab_switches": rules.max_tab_switches(),
            "show_result_immediately": rules.show_result_immediately(),
            "total_questions": exam.total_questions,
            "total_marks": exam.total_marks,
            "exam_mode": rules.exam_mode(),
        },
        "rules": public_rules,
        "sections": sections_out,
        "time_remaining_seconds": remaining,
    }


def _evaluate_attempt(attempt: Attempt, exam: Exam) -> None:
    eqs = {
        eq.question_id: eq
        for eq in ExamQuestion.query.filter_by(exam_id=exam.id).all()
    }
    answers = list(attempt.answers.all())
    answer_by_q = {a.question_id: a for a in answers}

    for qid, eq in eqs.items():
        if qid not in answer_by_q:
            a = AttemptAnswer(
                attempt_id=attempt.id,
                question_id=qid,
                exam_question_id=eq.id,
                section_id=eq.section_id,
                is_visited=False,
                is_answered=False,
            )
            db.session.add(a)
            answer_by_q[qid] = a
    db.session.flush()

    correct = wrong = skipped = attempted = 0
    score = 0.0
    neg_total = 0.0
    max_score = 0.0
    for eq in eqs.values():
        try:
            max_score += float(eq.marks or 0)
        except (TypeError, ValueError):
            pass

    section_stats: Dict[Any, Dict[str, Any]] = {}

    for qid, eq in eqs.items():
        a = answer_by_q[qid]
        q = eq.question
        if q is None:
            q = Question.query.get(qid)
        sid = eq.section_id if eq.section_id is not None else 0
        if sid not in section_stats:
            section_stats[sid] = {
                "section_id": eq.section_id,
                "total": 0,
                "attempted": 0,
                "correct": 0,
                "wrong": 0,
                "skipped": 0,
                "score": 0.0,
                "max_score": 0.0,
            }
        st = section_stats[sid]
        st["total"] += 1
        try:
            st["max_score"] += float(eq.marks or 0)
        except (TypeError, ValueError):
            pass

        if not a.is_answered or a.selected_answer in (None, ""):
            skipped += 1
            st["skipped"] += 1
            a.is_correct = None
            a.marks_awarded = 0.0
            continue

        attempted += 1
        st["attempted"] += 1
        neg_marks = float(eq.negative_marks or 0)
        try:
            eng = ExamRuleEngine.from_exam(exam)
            if not eng.negative_marking_enabled():
                neg_marks = 0.0
            partial = eng.partial_marking()
        except Exception:
            partial = False
        is_ok, marks = evaluate_answer(
            q.question_type if q else "single_choice",
            a.get_selected_parsed(),
            q.get_correct_answer_parsed() if q else None,
            float(eq.marks or 0),
            neg_marks,
            partial_marking=partial,
        )
        a.is_correct = bool(is_ok)
        a.marks_awarded = marks
        score += marks
        st["score"] += marks
        if is_ok:
            correct += 1
            st["correct"] += 1
        else:
            wrong += 1
            st["wrong"] += 1
            if marks < 0:
                neg_total += abs(marks)

    attempt.total_questions = len(eqs)
    attempt.attempted_count = attempted
    attempt.correct_count = correct
    attempt.wrong_count = wrong
    attempt.skipped_count = skipped
    attempt.score = round(score, 4)
    attempt.max_score = round(max_score, 4)
    attempt.percentage = round((score / max_score * 100) if max_score else 0.0, 2)
    attempt.negative_marks_total = round(neg_total, 4)
    attempt.set_section_results(list(section_stats.values()))
    attempt.status = "evaluated"
    attempt.submitted_at = attempt.submitted_at or _utcnow()
    if attempt.started_at and attempt.submitted_at:
        try:
            attempt.time_spent_seconds = max(
                0,
                int((attempt.submitted_at - attempt.started_at).total_seconds()),
            )
        except Exception:  # noqa: BLE001
            pass


def _result_payload(attempt: Attempt) -> Dict[str, Any]:
    exam_title = None
    try:
        exam_title = attempt.exam.title if attempt.exam else None
    except Exception:  # noqa: BLE001
        exam_title = None
    return {
        "attempt_id": attempt.id,
        "exam_id": attempt.exam_id,
        "exam_title": exam_title,
        "status": attempt.status,
        "score": attempt.score,
        "max_score": attempt.max_score,
        "percentage": attempt.percentage,
        "correct_count": attempt.correct_count,
        "wrong_count": attempt.wrong_count,
        "skipped_count": attempt.skipped_count,
        "attempted_count": attempt.attempted_count,
        "total_questions": attempt.total_questions,
        "negative_marks_total": attempt.negative_marks_total,
        "time_spent_seconds": attempt.time_spent_seconds,
        "section_results": attempt.get_section_results(),
        "submitted_at": (
            attempt.submitted_at.isoformat() if attempt.submitted_at else None
        ),
    }


def _auto_expire_if_needed(attempt: Attempt, exam: Exam) -> bool:
    """If in progress and past expires_at, evaluate and return True."""
    if attempt.status != "in_progress":
        return False
    if attempt.expires_at and attempt.expires_at < _utcnow():
        attempt.status = "auto_submitted"
        _evaluate_attempt(attempt, exam)
        return True
    return False


@attempts_bp.post("/start")
@jwt_required()
def start_attempt():
    data = _json_body()
    exam_id = data.get("exam_id")
    if exam_id is None:
        return jsonify({"error": "exam_id required"}), 400
    exam_id_i = _safe_int(exam_id)
    if exam_id_i is None:
        return jsonify({"error": "Invalid exam_id"}), 400

    exam = Exam.query.get_or_404(exam_id_i)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401

    user = User.query.get(user_id)
    allowed, reason = check_exam_access(user, exam)
    if not allowed:
        status = 403 if reason != "Authentication required" else 401
        return jsonify({"error": reason}), status

    if (exam.total_questions or 0) < 1:
        exam.recalculate_totals()
    if (exam.total_questions or 0) < 1:
        return jsonify({"error": "Exam has no questions"}), 400

    # No-repeat: if this is a file-bank test and the user has mastered some
    # questions before, refill the exam with FRESH questions of the same topic.
    try:
        has_active = Attempt.query.filter_by(
            exam_id=exam.id, user_id=user_id, status="in_progress"
        ).first()
        if not has_active:
            from app.services.exam_builder import rebuild_file_bank_exam
            rebuild_file_bank_exam(exam, user_id)
            db.session.commit()
            exam.recalculate_totals()
    except Exception:
        db.session.rollback()
        logger.exception("file-bank rebuild skipped for exam=%s", exam.id)

    rules = ExamRuleEngine.from_exam(exam)

    # Attempt limit from configuration (0 = unlimited)
    max_attempts = rules.max_attempts_per_user()
    if max_attempts > 0:
        finished = Attempt.query.filter(
            Attempt.exam_id == exam.id,
            Attempt.user_id == user_id,
            Attempt.status.in_(("submitted", "auto_submitted", "evaluated")),
        ).count()
        if finished >= max_attempts:
            return jsonify({"error": "Maximum attempts reached for this exam"}), 403

    existing = (
        Attempt.query.filter_by(exam_id=exam.id, user_id=user_id, status="in_progress")
        .order_by(Attempt.id.desc())
        .first()
    )
    if existing:
        if not rules.resume_allowed():
            return jsonify({"error": "Resume is not allowed for this exam configuration"}), 403
        if _auto_expire_if_needed(existing, exam):
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("auto-expire existing attempt failed")
        else:
            return jsonify(_build_player_payload(existing, exam))

    now = _utcnow()
    duration = int(rules.overall_seconds() or exam.duration_seconds or 3600)
    if duration < 1:
        duration = 3600

    attempt = Attempt(
        exam_id=exam.id,
        user_id=user_id,
        status="in_progress",
        started_at=now,
        expires_at=now + timedelta(seconds=duration),
        duration_seconds=duration,
        total_questions=exam.total_questions,
        max_score=exam.total_marks,
    )
    db.session.add(attempt)
    db.session.flush()

    for eq in ExamQuestion.query.filter_by(exam_id=exam.id).all():
        db.session.add(
            AttemptAnswer(
                attempt_id=attempt.id,
                question_id=eq.question_id,
                exam_question_id=eq.id,
                section_id=eq.section_id,
            )
        )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("start_attempt commit failed")
        return jsonify({"error": "Could not start attempt"}), 500
    return jsonify(_build_player_payload(attempt, exam)), 201


@attempts_bp.get("/<int:attempt_id>")
@jwt_required()
def get_attempt(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    denied = _authorize_attempt(attempt, user_id)
    if denied:
        return denied

    exam = attempt.exam
    if exam is None:
        return jsonify({"error": "Exam not found"}), 404

    if attempt.status == "in_progress":
        if _auto_expire_if_needed(attempt, exam):
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return jsonify(attempt.to_dict(include_answers=True))
        return jsonify(_build_player_payload(attempt, exam))
    return jsonify(attempt.to_dict(include_answers=True))


@attempts_bp.post("/<int:attempt_id>/answer")
@jwt_required()
def save_answer(attempt_id):
    """
    Body: {
      question_id, selected_answer?, is_marked_for_review?,
      time_spent_seconds?, is_visited?, clear?
    }
    """
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    if attempt.user_id != user_id:
        return jsonify({"error": "Forbidden"}), 403
    if attempt.status != "in_progress":
        return jsonify({"error": "Attempt is not in progress"}), 400

    exam = attempt.exam
    if exam is None:
        return jsonify({"error": "Exam not found"}), 404

    if attempt.expires_at and attempt.expires_at < _utcnow():
        attempt.status = "auto_submitted"
        _evaluate_attempt(attempt, exam)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({"error": "Time expired", "attempt": attempt.to_dict()}), 400

    data = _json_body()
    qid = _safe_int(data.get("question_id"))
    if qid is None:
        return jsonify({"error": "question_id required"}), 400

    ans = AttemptAnswer.query.filter_by(attempt_id=attempt.id, question_id=qid).first()
    if not ans:
        eq = ExamQuestion.query.filter_by(exam_id=attempt.exam_id, question_id=qid).first()
        if not eq:
            return jsonify({"error": "Question not in this exam"}), 404
        ans = AttemptAnswer(
            attempt_id=attempt.id,
            question_id=qid,
            exam_question_id=eq.id,
            section_id=eq.section_id,
        )
        db.session.add(ans)

    ans.is_visited = True
    if data.get("clear"):
        ans.set_selected(None)
        ans.changed_count = int(ans.changed_count or 0) + 1
    elif "selected_answer" in data:
        prev = ans.selected_answer
        ans.set_selected(data["selected_answer"])
        if prev != ans.selected_answer:
            ans.changed_count = int(ans.changed_count or 0) + 1
            ans.answered_at = _utcnow()

    if "is_marked_for_review" in data:
        ans.is_marked_for_review = bool(data["is_marked_for_review"])
    if "time_spent_seconds" in data:
        ts = _safe_int(data["time_spent_seconds"])
        if ts is not None and 0 <= ts <= _MAX_TIME_SPENT:
            ans.time_spent_seconds = max(int(ans.time_spent_seconds or 0), ts)
    if data.get("is_visited"):
        ans.is_visited = True

    if "current_section_index" in data:
        csi = _safe_int(data["current_section_index"])
        if csi is not None and csi >= 0:
            attempt.current_section_index = csi
    if "current_question_index" in data:
        cqi = _safe_int(data["current_question_index"])
        if cqi is not None and cqi >= 0:
            attempt.current_question_index = cqi

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("save_answer attempt=%s failed", attempt_id)
        return jsonify({"error": "Could not save answer"}), 500
    return jsonify({"message": "Answer saved", "answer": ans.to_dict()})


@attempts_bp.post("/<int:attempt_id>/answers/bulk")
@jwt_required()
def save_answers_bulk(attempt_id):
    """Body: { answers: [ {question_id, selected_answer, ...} ] }"""
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    if attempt.user_id != user_id:
        return jsonify({"error": "Forbidden"}), 403
    if attempt.status != "in_progress":
        return jsonify({"error": "Attempt is not in progress"}), 400

    if attempt.expires_at and attempt.expires_at < _utcnow():
        attempt.status = "auto_submitted"
        if attempt.exam:
            _evaluate_attempt(attempt, attempt.exam)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({"error": "Time expired"}), 400

    data = _json_body()
    items = data.get("answers") or []
    if not isinstance(items, list):
        return jsonify({"error": "answers must be a list"}), 400
    if len(items) > _MAX_BULK_ANSWERS:
        return jsonify({"error": f"At most {_MAX_BULK_ANSWERS} answers per bulk save"}), 400

    saved = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        qid = _safe_int(item.get("question_id"))
        if qid is None:
            continue
        ans = AttemptAnswer.query.filter_by(attempt_id=attempt.id, question_id=qid).first()
        if not ans:
            continue
        ans.is_visited = True
        if "selected_answer" in item:
            ans.set_selected(item["selected_answer"])
            ans.answered_at = _utcnow()
        if "is_marked_for_review" in item:
            ans.is_marked_for_review = bool(item["is_marked_for_review"])
        saved += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("bulk save attempt=%s failed", attempt_id)
        return jsonify({"error": "Could not save answers"}), 500
    return jsonify({"message": f"Saved {saved} answers"})


@attempts_bp.post("/<int:attempt_id>/security")
@jwt_required()
def report_security(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    if attempt.user_id != user_id:
        return jsonify({"error": "Forbidden"}), 403

    data = _json_body()
    event_type = str(data.get("type") or "unknown")[:64]
    message = data.get("message")
    if message is not None:
        message = str(message)[:_MAX_SECURITY_MSG]

    if event_type in ("TAB_CHANGE", "WINDOW_BLUR", "FULLSCREEN_EXIT"):
        attempt.tab_switch_count = int(attempt.tab_switch_count or 0) + 1

    attempt.add_security_flag({
        "type": event_type,
        "message": message,
        "timestamp": _utcnow().isoformat(),
    })

    force = False
    exam = attempt.exam
    rules = ExamRuleEngine.from_exam(exam) if exam else ExamRuleEngine.from_dict({})
    max_switches = rules.max_tab_switches() if exam else 999
    if (
        rules.detect_tab_switch()
        and rules.force_submit_on_max_tabs()
        and attempt.tab_switch_count >= max_switches
        and attempt.status == "in_progress"
        and exam is not None
    ):
        attempt.status = "auto_submitted"
        _evaluate_attempt(attempt, exam)
        force = True

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("report_security attempt=%s failed", attempt_id)
        return jsonify({"error": "Could not record security event"}), 500

    return jsonify({
        "message": "Security event recorded",
        "tab_switch_count": attempt.tab_switch_count,
        "force_submitted": force,
        "attempt": attempt.to_dict() if force else None,
    })


@attempts_bp.post("/<int:attempt_id>/submit")
@jwt_required()
def submit_attempt(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    if attempt.user_id != user_id:
        return jsonify({"error": "Forbidden"}), 403
    if attempt.status not in ("in_progress",):
        return jsonify({
            "error": "Attempt already submitted",
            "attempt": attempt.to_dict(),
        }), 400

    exam = attempt.exam
    if exam is None:
        return jsonify({"error": "Exam not found"}), 404

    data = _json_body()
    for item in (data.get("answers") or [])[:_MAX_BULK_ANSWERS]:
        if not isinstance(item, dict):
            continue
        qid = _safe_int(item.get("question_id"))
        if qid is None:
            continue
        ans = AttemptAnswer.query.filter_by(attempt_id=attempt.id, question_id=qid).first()
        if ans and "selected_answer" in item:
            ans.set_selected(item["selected_answer"])
            ans.is_visited = True

    auto = bool(data.get("auto", False))
    attempt.status = "auto_submitted" if auto else "submitted"
    attempt.submitted_at = _utcnow()
    _evaluate_attempt(attempt, exam)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("submit_attempt id=%s failed", attempt_id)
        return jsonify({"error": "Could not submit attempt"}), 500

    try:
        from app.services.notification_engine import notify

        exam_title = exam.title if exam else f"Exam {attempt.exam_id}"
        notify(
            user_id=attempt.user_id,
            category="exam_submitted",
            template_code="exam_submitted",
            variables={
                "name": attempt.user.full_name if attempt.user else "User",
                "exam": exam_title,
                "score": f"{attempt.score}/{attempt.max_score}",
            },
            channels=["in_app", "email"],
            data={"attempt_id": attempt.id, "exam_id": attempt.exam_id},
        )
    except Exception:
        logger.debug("submit notify skipped", exc_info=True)

    return jsonify({
        "message": "Exam submitted",
        "attempt": attempt.to_dict(include_answers=False),
        "result": _result_payload(attempt),
    })


@attempts_bp.get("/<int:attempt_id>/result")
@jwt_required()
def get_result(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    denied = _authorize_attempt(attempt, user_id)
    if denied:
        return denied
    if attempt.status == "in_progress":
        return jsonify({"error": "Attempt not yet submitted"}), 400
    return jsonify(_result_payload(attempt))


@attempts_bp.get("/<int:attempt_id>/review")
@jwt_required()
def get_review(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    denied = _authorize_attempt(attempt, user_id)
    if denied:
        return denied
    if attempt.status == "in_progress":
        return jsonify({"error": "Attempt not yet submitted"}), 400

    exam = attempt.exam
    if exam is None:
        return jsonify({"error": "Exam not found"}), 404

    rules = ExamRuleEngine.from_exam(exam)
    # Honor result visibility for students (admins always allowed)
    if not _is_admin() and not rules.show_result_immediately():
        return jsonify({
            "result": _result_payload(attempt),
            "items": [],
            "analytics": {},
            "message": "Detailed review is not available for this exam",
        })

    show_answers = rules.get("result.show_correct_answers", True)
    show_expl = rules.get("result.show_explanations", True)

    eqs = (
        ExamQuestion.query.filter_by(exam_id=exam.id)
        .order_by(ExamQuestion.order_index)
        .all()
    )
    answer_map = {a.question_id: a for a in attempt.answers.all()}
    items = []
    for eq in eqs:
        a = answer_map.get(eq.question_id)
        q = eq.question
        items.append({
            "exam_question_id": eq.id,
            "question_id": eq.question_id,
            "section_id": eq.section_id,
            "marks": eq.marks,
            "negative_marks": eq.negative_marks,
            "question": (
                q.to_dict(
                    include_answer=bool(show_answers),
                    include_explanation=bool(show_expl),
                )
                if q
                else None
            ),
            "selected_answer": a.get_selected_parsed() if a else None,
            "is_answered": bool(a.is_answered) if a else False,
            "is_correct": a.is_correct if a else None,
            "marks_awarded": a.marks_awarded if a else 0,
            "time_spent_seconds": a.time_spent_seconds if a else 0,
            "is_marked_for_review": bool(a.is_marked_for_review) if a else False,
        })

    analytics = {}
    try:
        analytics = build_attempt_analytics(attempt)
    except Exception:
        logger.exception("build_attempt_analytics failed attempt=%s", attempt_id)

    return jsonify({
        "result": _result_payload(attempt),
        "items": items,
        "analytics": analytics,
    })


@attempts_bp.get("")
@jwt_required()
def list_attempts():
    page, per_page = parse_pagination(request.args)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401

    q = Attempt.query
    if not _is_admin():
        q = q.filter_by(user_id=user_id)
    else:
        if request.args.get("user_id"):
            uid = _safe_int(request.args.get("user_id"))
            if uid is None:
                return jsonify({"error": "Invalid user_id"}), 400
            q = q.filter_by(user_id=uid)
    if request.args.get("exam_id"):
        eid = _safe_int(request.args.get("exam_id"))
        if eid is None:
            return jsonify({"error": "Invalid exam_id"}), 400
        q = q.filter_by(exam_id=eid)
    if request.args.get("status"):
        q = q.filter_by(status=str(request.args.get("status"))[:32])

    total = q.count()
    items = (
        q.order_by(Attempt.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return jsonify({
        "items": [a.to_dict() for a in items],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Idempotent submit with client submission_token
# - Server-side heartbeat remaining-time column
# - Row-level locking on submit to prevent double-eval races under multi-worker
# --------------------------------------------
