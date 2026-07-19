"""Basic analytics endpoints under ``/api/analytics``.

Students only see their own aggregates. Admins see platform-wide stats.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.attempt import Attempt
from app.models.exam import Exam
from app.models.question import Question
from app.models.subject import Subject
from app.models.user import User
from app.models.attempt import AttemptAnswer
from app.services.analytics_engine import build_attempt_analytics, build_user_analytics

analytics_bp = Blueprint("analytics", __name__)
logger = logging.getLogger("exam_os.routes.analytics")

_DONE_STATUSES = ("submitted", "auto_submitted", "evaluated")


def _identity_int():
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


def _is_admin() -> bool:
    try:
        return get_jwt().get("role") == "admin"
    except Exception:  # noqa: BLE001
        return False


@analytics_bp.get("/dashboard")
@jwt_required()
def dashboard():
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        from app.services.feature_flags import is_enabled

        if not is_enabled("ENABLE_ANALYTICS", True) and not _is_admin():
            return jsonify({
                "role": "student",
                "quick_stats": {},
                "message": "Analytics disabled by configuration",
            })
    except Exception:
        pass
    if _is_admin():
        return jsonify(_admin_dashboard())
    return jsonify(_student_dashboard(user_id))


def _student_dashboard(user_id: int) -> Dict[str, Any]:
    attempts = (
        Attempt.query.options(joinedload(Attempt.exam))
        .filter(
            Attempt.user_id == user_id,
            Attempt.status.in_(_DONE_STATUSES),
        )
        .order_by(Attempt.id.desc())
        .all()
    )

    total_tests = len(attempts)
    avg_score = (
        round(sum(float(a.percentage or 0) for a in attempts) / total_tests, 2)
        if total_tests
        else 0
    )
    best = max((float(a.percentage or 0) for a in attempts), default=0)
    total_correct = sum(int(a.correct_count or 0) for a in attempts)
    total_wrong = sum(int(a.wrong_count or 0) for a in attempts)
    total_q = sum(int(a.total_questions or 0) for a in attempts)
    accuracy = round(total_correct / total_q * 100, 2) if total_q else 0
    total_time = sum(int(a.time_spent_seconds or 0) for a in attempts)

    recent = [a.to_dict() for a in attempts[:10]]

    trend = []
    for a in reversed(attempts[-20:]):
        exam_title = f"Exam {a.exam_id}"
        try:
            if a.exam is not None:
                exam_title = a.exam.title
        except Exception:  # noqa: BLE001
            pass
        trend.append({
            "attempt_id": a.id,
            "exam_title": exam_title,
            "percentage": a.percentage,
            "score": a.score,
            "date": a.submitted_at.isoformat() if a.submitted_at else None,
        })

    subject_agg: Dict[str, Dict[str, float]] = {}
    for a in attempts:
        for sr in a.get_section_results():
            if not isinstance(sr, dict):
                continue
            key = str(sr.get("section_id") if sr.get("section_id") is not None else "general")
            bucket = subject_agg.setdefault(
                key, {"correct": 0, "total": 0, "score": 0, "max_score": 0}
            )
            try:
                bucket["correct"] += int(sr.get("correct") or 0)
                bucket["total"] += int(sr.get("total") or 0)
                bucket["score"] += float(sr.get("score") or 0)
                bucket["max_score"] += float(sr.get("max_score") or 0)
            except (TypeError, ValueError):
                continue

    history = {}
    try:
        history = build_user_analytics(user_id)
    except Exception:
        logger.exception("build_user_analytics failed user=%s", user_id)

    # Latest attempt deep analytics (additive)
    latest_deep = {}
    if attempts:
        try:
            latest_deep = build_attempt_analytics(attempts[0])
        except Exception:
            logger.exception("latest attempt analytics failed")

    return {
        "role": "student",
        "quick_stats": {
            "total_tests": total_tests,
            "average_percentage": avg_score,
            "best_percentage": best,
            "accuracy": accuracy,
            "total_correct": total_correct,
            "total_wrong": total_wrong,
            "total_questions_attempted": total_q,
            "total_time_seconds": total_time,
        },
        "recent_attempts": recent,
        "score_trend": trend,
        "section_aggregate": subject_agg,
        # Enterprise analytics (frontend may ignore unknown keys)
        "history": history.get("history") or [],
        "daily_progress": history.get("daily_progress") or [],
        "weekly_progress": history.get("weekly_progress") or [],
        "monthly_progress": history.get("monthly_progress") or [],
        "heatmap": history.get("heatmap") or [],
        "improvement": history.get("improvement") or 0,
        "latest_analytics": latest_deep,
        "ai_coach": latest_deep.get("ai_coach") if isinstance(latest_deep, dict) else {},
        "weak_subjects": latest_deep.get("weak_subjects") if isinstance(latest_deep, dict) else [],
        "strong_subjects": latest_deep.get("strong_subjects") if isinstance(latest_deep, dict) else [],
        "mistakes": latest_deep.get("mistakes") if isinstance(latest_deep, dict) else [],
        "revision_queue": latest_deep.get("revision_queue") if isinstance(latest_deep, dict) else [],
    }


def _admin_dashboard() -> Dict[str, Any]:
    users_count = User.query.count()
    students = User.query.filter_by(role="student").count()
    exams_count = Exam.query.count()
    published = Exam.query.filter_by(status="published").count()
    questions_count = Question.query.count()
    subjects_count = Subject.query.count()

    done_filter = Attempt.status.in_(_DONE_STATUSES)
    attempts_count = Attempt.query.filter(done_filter).count()
    avg_pct = (
        db.session.query(func.avg(Attempt.percentage)).filter(done_filter).scalar() or 0
    )

    recent = (
        Attempt.query.options(joinedload(Attempt.exam), joinedload(Attempt.user))
        .filter(done_filter)
        .order_by(Attempt.id.desc())
        .limit(15)
        .all()
    )

    by_exam = (
        db.session.query(
            Attempt.exam_id,
            func.count(Attempt.id),
            func.avg(Attempt.percentage),
        )
        .filter(done_filter)
        .group_by(Attempt.exam_id)
        .all()
    )

    exam_ids = [row[0] for row in by_exam if row[0] is not None]
    exam_map = {}
    if exam_ids:
        for ex in Exam.query.filter(Exam.id.in_(exam_ids)).all():
            exam_map[ex.id] = ex

    exam_stats: List[Dict[str, Any]] = []
    for exam_id, cnt, avg in by_exam:
        exam = exam_map.get(exam_id)
        exam_stats.append({
            "exam_id": exam_id,
            "title": exam.title if exam else str(exam_id),
            "attempts": cnt,
            "avg_percentage": round(float(avg or 0), 2),
        })

    return {
        "role": "admin",
        "quick_stats": {
            "users": users_count,
            "students": students,
            "exams": exams_count,
            "published_exams": published,
            "questions": questions_count,
            "subjects": subjects_count,
            "attempts": attempts_count,
            "average_percentage": round(float(avg_pct), 2),
        },
        "recent_attempts": [a.to_dict() for a in recent],
        "exam_stats": exam_stats,
    }


@analytics_bp.get("/exams/<int:exam_id>")
@jwt_required()
def exam_analytics(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401

    q = Attempt.query.filter(
        Attempt.exam_id == exam_id,
        Attempt.status.in_(_DONE_STATUSES),
    )
    if not _is_admin():
        q = q.filter_by(user_id=user_id)

    attempts = q.all()
    if not attempts:
        return jsonify({
            "exam_id": exam_id,
            "title": exam.title,
            "attempts": 0,
            "average_percentage": 0,
            "average_score": 0,
            "highest": 0,
            "lowest": 0,
        })

    pcts = [float(a.percentage or 0) for a in attempts]
    scores = [float(a.score or 0) for a in attempts]
    payload = {
        "exam_id": exam_id,
        "title": exam.title,
        "attempts": len(attempts),
        "average_percentage": round(sum(pcts) / len(pcts), 2),
        "average_score": round(sum(scores) / len(scores), 2),
        "highest": max(pcts),
        "lowest": min(pcts),
        "total_correct": sum(int(a.correct_count or 0) for a in attempts),
        "total_wrong": sum(int(a.wrong_count or 0) for a in attempts),
    }
    # Additive deep analytics for caller's latest attempt on this exam
    try:
        latest = max(attempts, key=lambda a: a.id)
        if _is_admin() or latest.user_id == user_id:
            payload["latest_analytics"] = build_attempt_analytics(latest)
    except Exception:
        logger.exception("exam latest analytics failed")
    return jsonify(payload)


@analytics_bp.get("/attempts/<int:attempt_id>")
@jwt_required()
def attempt_analytics(attempt_id):
    """Deep analytics for one attempt (additive endpoint)."""
    attempt = Attempt.query.get_or_404(attempt_id)
    user_id = _identity_int()
    if user_id is None:
        return jsonify({"error": "Unauthorized"}), 401
    if attempt.user_id != user_id and not _is_admin():
        return jsonify({"error": "Forbidden"}), 403
    if attempt.status not in _DONE_STATUSES:
        return jsonify({"error": "Attempt not yet submitted"}), 400
    try:
        return jsonify(build_attempt_analytics(attempt))
    except Exception:
        logger.exception("attempt_analytics failed")
        return jsonify({"error": "Analytics unavailable"}), 500


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Materialized daily stats table for large cohorts
# - Percentile / rank relative to all attempts on an exam
# --------------------------------------------
