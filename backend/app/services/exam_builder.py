"""
File-bank POOL selector (token-friendly, shared-test design).

Key idea
--------
A file-bank test is stored ONCE as a POOL of questions (all questions matching
the chosen subject/chapter/topic, answers taken from the file). The pool is
shared by every user and is never rebuilt per user -> no repeated AI calls.

Each ATTEMPT draws only ``questions_per_attempt`` questions from the pool:
  * A brand-new user gets N questions from the pool (all fresh for them).
  * A returning user gets N questions EXCLUDING the ones they already answered
    correctly in earlier attempts of the same exam -> "no repeat" behaviour,
    with zero AI cost (we just pick different pool questions).

If the user has mastered so many that fewer than N fresh remain, we top up with
already-seen ones so the paper always has the right number of questions.
"""
from __future__ import annotations

import logging
import random
import re
from typing import Dict, List, Optional, Set

from app.models.attempt import Attempt
from app.models.exam import Exam, ExamQuestion
from app.models.question import Question

logger = logging.getLogger("exam_os.services.exam_builder")


def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()[:400]


def _file_bank_cfg(exam: Exam) -> Optional[Dict]:
    rules = exam.get_rules() if hasattr(exam, "get_rules") else {}
    if not isinstance(rules, dict):
        return None
    cfg = rules.get("file_bank_source")
    return cfg if isinstance(cfg, dict) else None


def questions_per_attempt(exam: Exam, fallback: int = 0) -> int:
    """How many questions each attempt should show (0 => use whole pool)."""
    cfg = _file_bank_cfg(exam)
    if cfg:
        try:
            n = int(cfg.get("questions_per_attempt") or 0)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return fallback


def mastered_question_ids(user_id: int, exam_id: int) -> Set[int]:
    """
    Question ids this user has EVER answered correctly in this exam,
    matched by normalized text so identical questions count as mastered even
    across different Question rows.
    """
    mastered_texts: Set[str] = set()
    attempts = Attempt.query.filter(
        Attempt.user_id == user_id,
        Attempt.exam_id == exam_id,
        Attempt.status.in_(("submitted", "auto_submitted", "evaluated")),
    ).all()
    for att in attempts:
        for ans in att.answers.all():
            if ans.is_correct:
                q = Question.query.get(ans.question_id)
                if q:
                    mastered_texts.add(normalize_text(q.question_text))
    if not mastered_texts:
        return set()

    mastered_ids: Set[int] = set()
    for eq in ExamQuestion.query.filter_by(exam_id=exam_id).all():
        q = eq.question or Question.query.get(eq.question_id)
        if q and normalize_text(q.question_text) in mastered_texts:
            mastered_ids.add(eq.question_id)
    return mastered_ids


def select_attempt_questions(exam: Exam, user_id: int) -> List[ExamQuestion]:
    """
    Return the list of ExamQuestion to include in a NEW attempt.

    * Not a file-bank/pool exam -> return all exam questions (unchanged behaviour).
    * Pool exam -> return N questions, preferring ones the user hasn't mastered.
    """
    all_eqs = (
        ExamQuestion.query.filter_by(exam_id=exam.id)
        .order_by(ExamQuestion.order_index)
        .all()
    )
    n = questions_per_attempt(exam, fallback=0)
    if n <= 0 or n >= len(all_eqs):
        # Whole pool is the paper (small bank) -> everyone gets all of it.
        return all_eqs

    try:
        mastered = mastered_question_ids(user_id, exam.id)
    except Exception:
        logger.exception("mastered lookup failed exam=%s user=%s", exam.id, user_id)
        mastered = set()

    fresh = [eq for eq in all_eqs if eq.question_id not in mastered]
    seen = [eq for eq in all_eqs if eq.question_id in mastered]

    random.shuffle(fresh)
    random.shuffle(seen)

    chosen = fresh[:n]
    if len(chosen) < n:
        # Not enough fresh -> top up with already-seen so paper size stays real.
        chosen += seen[: (n - len(chosen))]

    # Preserve original order for a clean paper feel
    chosen_ids = {eq.id for eq in chosen}
    return [eq for eq in all_eqs if eq.id in chosen_ids]
