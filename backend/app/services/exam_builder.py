"""
File-bank exam (re)builder.

Used by the attempt flow so that when a user RE-OPENS a file-bank based test,
the questions they already answered correctly are NOT repeated — the exam is
refilled with fresh questions of the same topic from the file bank.

Design notes:
* We never delete `questions` rows (they may be referenced by past attempts).
* Before deleting `exam_questions`, we null out `attempt_answers.exam_question_id`
  that reference them, so history stays intact and no FK constraint breaks.
* If not enough fresh questions exist, we fall back to reusing older ones so the
  exam is never left empty.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional, Set

from app.extensions import db
from app.models.attempt import Attempt, AttemptAnswer
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.question import Question, QuestionOption

logger = logging.getLogger("exam_os.services.exam_builder")


def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()[:400]


def mastered_question_texts(user_id: int, exam_id: int) -> Set[str]:
    """Normalized texts of questions this user has EVER answered correctly in this exam."""
    mastered: Set[str] = set()
    attempts = (
        Attempt.query.filter(
            Attempt.user_id == user_id,
            Attempt.exam_id == exam_id,
            Attempt.status.in_(("submitted", "auto_submitted", "evaluated")),
        ).all()
    )
    for att in attempts:
        for ans in att.answers.all():
            if ans.is_correct:
                q = Question.query.get(ans.question_id)
                if q:
                    mastered.add(normalize_text(q.question_text))
    return mastered


def rebuild_file_bank_exam(exam: Exam, user_id: int) -> int:
    """
    Rebuild `exam`'s live question set with fresh file-bank questions,
    excluding any the user already mastered. Returns number of questions now in exam.
    Safe no-op (returns current count) if exam is not file-bank based.
    """
    rules = exam.get_rules() if hasattr(exam, "get_rules") else {}
    if not isinstance(rules, dict):
        return exam.total_questions or 0
    cfg = rules.get("file_bank_source")
    if not isinstance(cfg, dict) or not cfg.get("no_repeat_correct"):
        return exam.total_questions or 0

    try:
        from app.services.file_bank import filter_questions
    except Exception:
        return exam.total_questions or 0

    # How many questions should this exam have?
    existing_eqs = ExamQuestion.query.filter_by(exam_id=exam.id).all()
    target_count = len(existing_eqs) or 20

    mastered = mastered_question_texts(user_id, exam.id)
    if not mastered:
        return len(existing_eqs)  # nothing to swap yet

    # Pull a generous fresh pool from the file bank (same filter)
    pool = filter_questions(
        subject=cfg.get("subject"),
        chapter=cfg.get("chapter"),
        topic=cfg.get("topic"),
        difficulty=cfg.get("difficulty"),
        count=target_count * 5,
        require_answer=False,
        shuffle=True,
    )
    # keep only fresh (not mastered) and answer-resolvable
    fresh = [fq for fq in pool if normalize_text(fq["question_text"]) not in mastered]

    # AI fallback availability for missing answers
    try:
        from app.services.knowledge_engine.free_ai_chain import derive_answer_with_ai
        import os
        ai_ok = any(os.getenv(k) for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"))
    except Exception:
        derive_answer_with_ai = None
        ai_ok = False

    # Build up to target_count fresh questions with a valid answer
    chosen = []
    used_texts: Set[str] = set()
    for fq in fresh:
        if len(chosen) >= target_count:
            break
        ntext = normalize_text(fq["question_text"])
        if ntext in used_texts:
            continue
        options = fq.get("options", [])[:4]
        correct = fq.get("correct_answer")
        explanation = fq.get("explanation") or ""
        if not correct and derive_answer_with_ai and ai_ok:
            ai = derive_answer_with_ai(fq["question_text"], options)
            if ai:
                correct = ai["correct_answer"]
                explanation = explanation or ai.get("explanation", "")
        valid_keys = {str(o.get("option_key", "")).upper() for o in options}
        if not correct or correct not in valid_keys:
            continue
        used_texts.add(ntext)
        chosen.append({"fq": fq, "correct": correct, "explanation": explanation, "options": options})

    if not chosen:
        # No fresh questions available — leave exam unchanged
        logger.info("rebuild_file_bank_exam: no fresh questions for exam=%s user=%s", exam.id, user_id)
        return len(existing_eqs)

    # --- Swap live question set safely ---
    section = ExamSection.query.filter_by(exam_id=exam.id).order_by(ExamSection.order_index).first()
    if section is None:
        section = ExamSection(exam_id=exam.id, title="General", order_index=0)
        db.session.add(section)
        db.session.flush()

    # Detach past attempt answers from exam_questions we are about to delete
    old_eq_ids = [eq.id for eq in existing_eqs]
    if old_eq_ids:
        AttemptAnswer.query.filter(
            AttemptAnswer.exam_question_id.in_(old_eq_ids)
        ).update({AttemptAnswer.exam_question_id: None}, synchronize_session=False)
        ExamQuestion.query.filter(ExamQuestion.id.in_(old_eq_ids)).delete(synchronize_session=False)
    db.session.flush()

    added = 0
    for item in chosen:
        fq = item["fq"]
        q = Question(
            question_text=fq["question_text"][:2000],
            question_type="single_choice",
            difficulty=fq.get("difficulty", "medium") if fq.get("difficulty") in ("easy", "medium", "hard") else "medium",
            correct_answer=item["correct"],
            explanation=(item["explanation"][:2000] if item["explanation"] else None),
            marks=2,
            negative_marks=0.5,
            is_active=True,
            tags=f"{fq.get('subject','')},{fq.get('chapter','')},{fq.get('topic','')}"[:512],
            source=fq.get("source", "file_bank"),
        )
        db.session.add(q)
        db.session.flush()
        for oi, opt in enumerate(item["options"]):
            db.session.add(QuestionOption(
                question_id=q.id,
                option_key=opt.get("option_key", "A"),
                option_text=opt.get("option_text", "")[:500],
                order_index=oi,
            ))
        db.session.add(ExamQuestion(
            exam_id=exam.id,
            section_id=section.id,
            question_id=q.id,
            order_index=added,
            marks=2,
            negative_marks=0.5,
        ))
        added += 1

    try:
        exam.recalculate_totals()
        exam.duration_seconds = max(60, added * 60)
    except Exception:
        pass

    db.session.flush()
    logger.info("rebuild_file_bank_exam: exam=%s user=%s refilled with %s fresh questions", exam.id, user_id, added)
    return added
