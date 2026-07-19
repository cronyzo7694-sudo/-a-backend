"""Enterprise analytics engine — derives insights from attempts + answers.

Public::

    build_attempt_analytics(attempt) -> dict
    build_user_analytics(user_id) -> dict
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.attempt import Attempt, AttemptAnswer
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.question import Question
from app.models.subject import Subject
from app.models.chapter import Chapter

logger = logging.getLogger("exam_os.services.analytics_engine")

_DONE = ("submitted", "auto_submitted", "evaluated")


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_div(n: float, d: float, default: float = 0.0) -> float:
    if not d:
        return default
    return n / d


def _guess_heuristic(time_spent: int, is_correct: Optional[bool], changed_count: int) -> bool:
    """Cheap guess detector: very fast wrong answers with few changes."""
    if is_correct is True:
        return False
    if time_spent is None:
        return False
    if time_spent <= 8 and (changed_count or 0) <= 1 and is_correct is False:
        return True
    return False


def build_attempt_analytics(attempt: Attempt) -> Dict[str, Any]:
    """Deep analysis for a single finished attempt."""
    if attempt is None:
        return {}

    exam = attempt.exam
    answers = (
        AttemptAnswer.query.options(joinedload(AttemptAnswer.question))
        .filter_by(attempt_id=attempt.id)
        .all()
    )
    eqs = {
        eq.question_id: eq
        for eq in ExamQuestion.query.filter_by(exam_id=attempt.exam_id).all()
    }
    sections = {
        s.id: s for s in (exam.sections if exam else [])
    }

    total = len(eqs) or attempt.total_questions or 0
    correct = int(attempt.correct_count or 0)
    wrong = int(attempt.wrong_count or 0)
    skipped = int(attempt.skipped_count or 0)
    attempted = int(attempt.attempted_count or 0)

    accuracy = round(_safe_div(correct, attempted) * 100, 2) if attempted else 0.0
    attempt_rate = round(_safe_div(attempted, total) * 100, 2) if total else 0.0

    times: List[int] = []
    by_subject: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "correct": 0, "wrong": 0, "skipped": 0, "attempted": 0,
            "time": 0, "marks": 0.0, "max_marks": 0.0, "guesses": 0,
        }
    )
    by_chapter: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "correct": 0, "wrong": 0, "skipped": 0, "attempted": 0,
            "time": 0, "name": "",
        }
    )
    by_section: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "correct": 0, "wrong": 0, "skipped": 0, "attempted": 0,
            "time": 0, "score": 0.0, "max_score": 0.0, "title": "",
        }
    )
    by_difficulty: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"correct": 0, "wrong": 0, "skipped": 0, "attempted": 0}
    )

    wrong_pattern = {"fast_wrong": 0, "slow_wrong": 0, "changed_then_wrong": 0}
    guess_count = 0
    score_loss_wrong = 0.0
    score_loss_negative = float(attempt.negative_marks_total or 0)
    score_loss_skipped = 0.0
    question_rows: List[Dict[str, Any]] = []

    ans_by_q = {a.question_id: a for a in answers}

    for qid, eq in eqs.items():
        a = ans_by_q.get(qid)
        q = eq.question or (a.question if a else None) or Question.query.get(qid)
        t = int(a.time_spent_seconds or 0) if a else 0
        times.append(t)
        marks = float(eq.marks or 0)
        neg = float(eq.negative_marks or 0)

        subj = "Unknown"
        chap = "Unknown"
        diff = "medium"
        if q is not None:
            try:
                subj = q.subject.name if q.subject else f"subject:{q.subject_id}"
            except Exception:
                subj = f"subject:{getattr(q, 'subject_id', None)}"
            try:
                chap = q.chapter.name if q.chapter else f"chapter:{q.chapter_id}"
            except Exception:
                chap = f"chapter:{getattr(q, 'chapter_id', None)}"
            diff = (q.difficulty or "medium").lower()

        sec_key = str(eq.section_id or "none")
        sec_title = ""
        if eq.section_id and eq.section_id in sections:
            sec_title = sections[eq.section_id].title
        by_section[sec_key]["title"] = sec_title or sec_key
        by_section[sec_key]["max_score"] += marks
        by_section[sec_key]["time"] += t

        by_subject[subj]["max_marks"] += marks
        by_subject[subj]["time"] += t
        by_chapter[chap]["name"] = chap
        by_chapter[chap]["time"] += t

        is_corr = a.is_correct if a else None
        is_ans = bool(a and a.is_answered)
        awarded = float(a.marks_awarded or 0) if a else 0.0
        changed = int(a.changed_count or 0) if a else 0
        guess = _guess_heuristic(t, is_corr, changed)
        if guess:
            guess_count += 1
            by_subject[subj]["guesses"] += 1

        if not is_ans:
            skipped_n = 1
            by_subject[subj]["skipped"] += 1
            by_chapter[chap]["skipped"] += 1
            by_section[sec_key]["skipped"] += 1
            by_difficulty[diff]["skipped"] += 1
            score_loss_skipped += marks
        elif is_corr:
            by_subject[subj]["correct"] += 1
            by_subject[subj]["attempted"] += 1
            by_subject[subj]["marks"] += awarded
            by_chapter[chap]["correct"] += 1
            by_chapter[chap]["attempted"] += 1
            by_section[sec_key]["correct"] += 1
            by_section[sec_key]["attempted"] += 1
            by_section[sec_key]["score"] += awarded
            by_difficulty[diff]["correct"] += 1
            by_difficulty[diff]["attempted"] += 1
        else:
            by_subject[subj]["wrong"] += 1
            by_subject[subj]["attempted"] += 1
            by_subject[subj]["marks"] += awarded
            by_chapter[chap]["wrong"] += 1
            by_chapter[chap]["attempted"] += 1
            by_section[sec_key]["wrong"] += 1
            by_section[sec_key]["attempted"] += 1
            by_section[sec_key]["score"] += awarded
            by_difficulty[diff]["wrong"] += 1
            by_difficulty[diff]["attempted"] += 1
            score_loss_wrong += marks
            if t and t <= 15:
                wrong_pattern["fast_wrong"] += 1
            if t and t >= 90:
                wrong_pattern["slow_wrong"] += 1
            if changed >= 2:
                wrong_pattern["changed_then_wrong"] += 1

        question_rows.append({
            "question_id": qid,
            "time_spent_seconds": t,
            "is_correct": is_corr,
            "is_answered": is_ans,
            "marks_awarded": awarded,
            "marks": marks,
            "subject": subj,
            "chapter": chap,
            "difficulty": diff,
            "guess": guess,
            "changed_count": changed,
        })

    avg_time = round(mean(times), 2) if times else 0.0
    med_time = round(median(times), 2) if times else 0.0
    total_time = int(attempt.time_spent_seconds or sum(times) or 0)
    speed_qpm = round(_safe_div(attempted * 60.0, total_time), 2) if total_time else 0.0

    # Weak / strong areas
    weak_subjects = []
    strong_subjects = []
    for name, st in by_subject.items():
        att = st["attempted"]
        acc = round(_safe_div(st["correct"], att) * 100, 2) if att else 0.0
        row = {
            "name": name,
            "accuracy": acc,
            "attempted": att,
            "correct": st["correct"],
            "wrong": st["wrong"],
            "skipped": st["skipped"],
            "time_seconds": st["time"],
            "score": round(st["marks"], 2),
            "max_marks": round(st["max_marks"], 2),
            "guesses": st["guesses"],
        }
        if att >= 1 and acc < 50:
            weak_subjects.append(row)
        if att >= 1 and acc >= 75:
            strong_subjects.append(row)
    weak_subjects.sort(key=lambda x: x["accuracy"])
    strong_subjects.sort(key=lambda x: -x["accuracy"])

    weak_chapters = []
    for name, st in by_chapter.items():
        att = st["attempted"]
        acc = round(_safe_div(st["correct"], att) * 100, 2) if att else 0.0
        if att >= 1 and acc < 50:
            weak_chapters.append({
                "name": name,
                "accuracy": acc,
                "attempted": att,
                "correct": st["correct"],
                "wrong": st["wrong"],
                "time_seconds": st["time"],
            })
    weak_chapters.sort(key=lambda x: x["accuracy"])

    # Rank / percentile prediction vs other attempts on same exam
    percentile = None
    rank_prediction = None
    peer_count = 0
    try:
        peers = (
            Attempt.query.filter(
                Attempt.exam_id == attempt.exam_id,
                Attempt.status.in_(_DONE),
            ).all()
        )
        peer_count = len(peers)
        if peer_count >= 2:
            scores = sorted([float(p.percentage or 0) for p in peers])
            my = float(attempt.percentage or 0)
            below = sum(1 for s in scores if s < my)
            percentile = round(below / peer_count * 100, 2)
            better = sum(1 for s in scores if s > my)
            rank_prediction = better + 1
    except Exception:
        logger.exception("percentile calc failed")

    max_score = float(attempt.max_score or 0)
    potential = max_score
    actual = float(attempt.score or 0)
    score_loss = round(max(0.0, potential - actual), 2)

    # Attempt quality 0-100
    quality = round(
        min(
            100.0,
            accuracy * 0.5
            + attempt_rate * 0.2
            + max(0, 30 - guess_count * 3)
            + (10 if float(attempt.percentage or 0) >= 60 else 0),
        ),
        2,
    )

    suggestions: List[str] = []
    if accuracy < 60:
        suggestions.append("Focus on accuracy: revise weak chapters before attempting more mocks.")
    if guess_count >= 3:
        suggestions.append("Reduce guessing: skip uncertain items and return if time permits.")
    if wrong_pattern["fast_wrong"] >= 2:
        suggestions.append("Slow down on easy-looking questions — fast wrongs detected.")
    if wrong_pattern["slow_wrong"] >= 2:
        suggestions.append("Avoid time sinks: set a per-question cap and mark for review.")
    if score_loss_negative >= 2:
        suggestions.append("Negative marking is hurting you — attempt only high-confidence items.")
    if weak_subjects:
        suggestions.append(
            f"Priority weak subject: {weak_subjects[0]['name']} ({weak_subjects[0]['accuracy']}% accuracy)."
        )
    if not suggestions:
        suggestions.append("Solid attempt — schedule a timed mock to convert accuracy into speed.")

    ai_coach = {
        "summary": (
            f"Score {actual}/{max_score} ({attempt.percentage}%). "
            f"Accuracy {accuracy}% on {attempted}/{total} attempted. "
            f"Estimated quality {quality}/100."
        ),
        "focus_areas": [w["name"] for w in weak_subjects[:3]],
        "suggestions": suggestions[:6],
        "next_best_action": (
            f"Practice 20 questions from {weak_subjects[0]['name']}"
            if weak_subjects
            else "Take another full mock under the same rule pack"
        ),
    }

    # Mistake notebook + revision queue from wrong/skipped
    mistakes = []
    revision_queue = []
    for row in question_rows:
        if row["is_correct"] is False or (not row["is_answered"]):
            entry = {
                "question_id": row["question_id"],
                "subject": row["subject"],
                "chapter": row["chapter"],
                "difficulty": row["difficulty"],
                "reason": "wrong" if row["is_correct"] is False else "skipped",
                "guess": row["guess"],
                "time_spent_seconds": row["time_spent_seconds"],
            }
            if row["is_correct"] is False:
                mistakes.append(entry)
            revision_queue.append(entry)

    return {
        "attempt_id": attempt.id,
        "exam_id": attempt.exam_id,
        "accuracy": accuracy,
        "attempt_rate": attempt_rate,
        "speed_qpm": speed_qpm,
        "avg_time_per_question": avg_time,
        "median_time_per_question": med_time,
        "total_time_seconds": total_time,
        "guess_count": guess_count,
        "guess_rate": round(_safe_div(guess_count, attempted) * 100, 2) if attempted else 0,
        "wrong_pattern": wrong_pattern,
        "score_loss": {
            "total": score_loss,
            "from_wrong": round(score_loss_wrong, 2),
            "from_negative": round(score_loss_negative, 2),
            "from_skipped": round(score_loss_skipped, 2),
        },
        "attempt_quality": quality,
        "percentile": percentile,
        "rank_prediction": rank_prediction,
        "peer_count": peer_count,
        "by_subject": list(by_subject_items(by_subject)),
        "by_chapter": weak_chapters + [
            {
                "name": n,
                "accuracy": round(_safe_div(s["correct"], s["attempted"]) * 100, 2) if s["attempted"] else 0,
                "attempted": s["attempted"],
                "correct": s["correct"],
                "wrong": s["wrong"],
                "time_seconds": s["time"],
            }
            for n, s in by_chapter.items()
            if s["attempted"] >= 1 and round(_safe_div(s["correct"], s["attempted"]) * 100, 2) >= 50
        ][:20],
        "by_section": [
            {
                "section_id": k,
                "title": v["title"],
                "correct": v["correct"],
                "wrong": v["wrong"],
                "skipped": v["skipped"],
                "attempted": v["attempted"],
                "time_seconds": v["time"],
                "score": round(v["score"], 2),
                "max_score": round(v["max_score"], 2),
                "accuracy": round(_safe_div(v["correct"], v["attempted"]) * 100, 2) if v["attempted"] else 0,
            }
            for k, v in by_section.items()
        ],
        "by_difficulty": {
            k: {
                **v,
                "accuracy": round(_safe_div(v["correct"], v["attempted"]) * 100, 2) if v["attempted"] else 0,
            }
            for k, v in by_difficulty.items()
        },
        "weak_subjects": weak_subjects[:10],
        "strong_subjects": strong_subjects[:10],
        "weak_chapters": weak_chapters[:15],
        "ai_coach": ai_coach,
        "suggestions": suggestions,
        "mistakes": mistakes[:50],
        "revision_queue": revision_queue[:50],
        "question_timing": question_rows,
    }


def by_subject_items(by_subject: Dict[str, Dict[str, Any]]):
    for name, st in by_subject.items():
        att = st["attempted"]
        yield {
            "name": name,
            "correct": st["correct"],
            "wrong": st["wrong"],
            "skipped": st["skipped"],
            "attempted": att,
            "time_seconds": st["time"],
            "score": round(st["marks"], 2),
            "max_marks": round(st["max_marks"], 2),
            "accuracy": round(_safe_div(st["correct"], att) * 100, 2) if att else 0,
            "guesses": st["guesses"],
            "avg_time": round(_safe_div(st["time"], att), 2) if att else 0,
        }


def build_user_analytics(user_id: int) -> Dict[str, Any]:
    """Historical trends for a student across finished attempts."""
    attempts = (
        Attempt.query.options(joinedload(Attempt.exam))
        .filter(Attempt.user_id == user_id, Attempt.status.in_(_DONE))
        .order_by(Attempt.id.asc())
        .all()
    )
    if not attempts:
        return {
            "history": [],
            "daily_progress": [],
            "weekly_progress": [],
            "monthly_progress": [],
            "subject_trend": {},
            "heatmap": [],
            "improvement": 0,
        }

    history = []
    daily: Dict[str, List[float]] = defaultdict(list)
    weekly: Dict[str, List[float]] = defaultdict(list)
    monthly: Dict[str, List[float]] = defaultdict(list)

    for a in attempts:
        pct = float(a.percentage or 0)
        dt = a.submitted_at or a.started_at or _utcnow()
        day = dt.strftime("%Y-%m-%d")
        week = dt.strftime("%Y-W%W")
        month = dt.strftime("%Y-%m")
        daily[day].append(pct)
        weekly[week].append(pct)
        monthly[month].append(pct)
        title = a.exam.title if a.exam else f"Exam {a.exam_id}"
        history.append({
            "attempt_id": a.id,
            "exam_id": a.exam_id,
            "exam_title": title,
            "percentage": pct,
            "score": a.score,
            "accuracy": round(
                _safe_div(int(a.correct_count or 0), int(a.attempted_count or 0)) * 100, 2
            ) if a.attempted_count else 0,
            "date": dt.isoformat(),
        })

    def series(bucket: Dict[str, List[float]]):
        return [
            {"period": k, "avg_percentage": round(mean(v), 2), "attempts": len(v)}
            for k, v in sorted(bucket.items())
        ]

    first = float(attempts[0].percentage or 0)
    last = float(attempts[-1].percentage or 0)
    improvement = round(last - first, 2)

    # Simple heatmap: weekday x hour counts
    heat = defaultdict(int)
    for a in attempts:
        dt = a.submitted_at or a.started_at
        if not dt:
            continue
        heat[f"{dt.weekday()}-{dt.hour}"] += 1
    heatmap = [
        {"weekday": int(k.split("-")[0]), "hour": int(k.split("-")[1]), "count": v}
        for k, v in heat.items()
    ]

    return {
        "history": history[-50:],
        "daily_progress": series(daily)[-60:],
        "weekly_progress": series(weekly)[-26:],
        "monthly_progress": series(monthly)[-24:],
        "improvement": improvement,
        "heatmap": heatmap,
        "totals": {
            "attempts": len(attempts),
            "avg_percentage": round(mean([float(a.percentage or 0) for a in attempts]), 2),
            "best_percentage": max(float(a.percentage or 0) for a in attempts),
        },
    }
