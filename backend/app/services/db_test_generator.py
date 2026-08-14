"""
DB-driven test generator (File-Bank style, but from the Neon `questions` table).

Creates child tests inside an exam — topic-wise, chapter-wise, subject-wise and
a full mock — exactly like the file-bank `auto_test` flow, but the question
pool comes from the `questions` table instead of .txt files. Existing DB
questions are REUSED (no duplicate Question rows are created).

Usage (admin):
    POST /api/admin/generate-db-tests/<exam_id>
    POST /api/admin/generate-db-tests            # all top-level exams
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from app.extensions import db
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.question import Question, QuestionOption
from app.models.subject import Subject
from app.models.chapter import Chapter
from app.models.bank import Topic

logger = logging.getLogger("exam_os.services.db_test_generator")

# How many questions an attempt should show per scope.
# (User spec: topic test = 10, chapter test = 15, subject test = 25,
#  full test = as many as the REAL exam pattern has.)
PER_ATTEMPT = {"topic_wise": 10, "chapter_wise": 15, "subject_wise": 25, "full_mock": 0}
_MIN_TOPIC = 5      # minimum questions to build a topic test
_MIN_CHAPTER = 8    # minimum questions to build a chapter test
_MAX_POOL = 500

# REAL full-mock question counts per exam (by matching title keywords).
REAL_FULL_COUNTS = {
    "ssc cgl tier-1": 100,
    "ssc cgl tier-2": 150,
    "ssc chsl": 100,
    "ssc mts": 90,
    "ssc gd": 80,
    "ssc cpo": 200,
    "ssc je": 200,
    "ssc stenographer": 200,
    "ssc selection post": 100,
}


def _real_full_count(exam_title: str) -> int:
    """Return the real exam's total question count for a full mock, or 0 if
    unknown (caller falls back to available pool size)."""
    t = (exam_title or "").lower()
    # order matters: check longer/more specific keys first
    for key, count in REAL_FULL_COUNTS.items():
        if key in t:
            return count
    return 0


def _per_attempt(scope: str, exam: Exam, pool_size: int) -> int:
    """Decide how many questions an attempt shows.
    - topic/chapter/subject: fixed counts (10 / 15 / 25)
    - full_mock: the real exam's total question count (capped at pool size)."""
    if scope == "full_mock":
        want = _real_full_count(exam.title)
        if want <= 0:
            want = pool_size  # fallback: show everything we have
        return min(want, pool_size)
    n = PER_ATTEMPT.get(scope, 0)
    if n <= 0:
        return pool_size
    return min(n, pool_size)


def _canon(subject: Optional[str], chapter: Optional[str]) -> Tuple[str, str]:
    return ((subject or "").strip().lower(), (chapter or "").strip().lower())


def _db_inventory() -> Dict:
    """Inventory of DB questions grouped by subject / chapter / topic."""
    topics: Dict[Tuple[str, str, str], int] = defaultdict(int)
    chapters: Dict[Tuple[str, str], int] = defaultdict(int)
    subjects: Dict[str, int] = defaultdict(int)

    qs = Question.query.filter_by(is_active=True).all()
    # Resolve topic names from the topics table (Question has topic_id, not a rel).
    topic_names = {}
    for q in qs:
        if q.topic_id and q.topic_id not in topic_names:
            t = Topic.query.get(q.topic_id)
            topic_names[q.topic_id] = t.name if t else None
    for q in qs:
        subj = (q.subject.name if q.subject else "General")
        chap = (q.chapter.name if q.chapter else "General")
        topic = topic_names.get(q.topic_id) or chap
        topics[(subj, chap, topic)] += 1
        chapters[(subj, chap)] += 1
        subjects[subj] += 1
    return {"topics": dict(topics), "chapters": dict(chapters), "subjects": dict(subjects)}


def _db_pool(subject: Optional[str], chapter: Optional[str], topic: Optional[str]) -> List[Question]:
    """Return active DB questions matching subject/chapter/topic."""
    query = Question.query.filter_by(is_active=True)
    if subject:
        query = query.join(Subject, Question.subject_id == Subject.id).filter(Subject.name.ilike(subject))
    if chapter or topic:
        query = query.join(Chapter, Question.chapter_id == Chapter.id)
        if chapter:
            query = query.filter(Chapter.name.ilike(chapter))
        if topic:
            query = query.filter(Chapter.name.ilike(topic))
    return query.limit(_MAX_POOL).all()


def _auto_key(scope: str, subject: str, chapter: str, topic: str) -> str:
    return "|".join([scope, subject, chapter, topic]).lower()


def _build_test(exam: Exam, scope: str, subject: Optional[str],
                chapter: Optional[str], topic: Optional[str],
                auto_key: str, title: str) -> Optional[Exam]:
    pool = _db_pool(subject, chapter, topic)
    # filter to answerable
    answerable = [q for q in pool if q.correct_answer]
    if not answerable:
        return None
    min_needed = _MIN_TOPIC if scope == "topic_wise" else _MIN_CHAPTER
    if len(answerable) < min_needed:
        return None

    # Decide how many questions this attempt shows (topic=10, chapter=15,
    # subject=25, full=real exam count), capped by how many we actually have.
    per_attempt = _per_attempt(scope, exam, len(answerable))
    if per_attempt <= 0:
        per_attempt = len(answerable)

    test_mode = "pyq" if any(q.source and q.source.strip() for q in answerable) else "mock"

    child = Exam(
        title=title[:255],
        description=f"Auto {scope.replace('_', ' ')} for {exam.title}",
        duration_seconds=per_attempt * 60,
        status="published",
        exam_mode=test_mode,
        default_marks=2,
        default_negative_marks=0.5,
        parent_exam_id=exam.id,
    )
    db.session.add(child)
    db.session.flush()

    section = ExamSection(exam_id=child.id, title=chapter or subject or topic or "General", order_index=0)
    db.session.add(section)
    db.session.flush()

    # Pick a fresh shuffled subset of `per_attempt` questions, assigning each
    # to the child test. Reuses existing DB questions (no duplicates created).
    import random
    chosen = random.sample(answerable, min(per_attempt, len(answerable)))
    added = 0
    for q in chosen:
        db.session.add(ExamQuestion(
            exam_id=child.id, section_id=section.id, question_id=q.id,
            order_index=added, marks=float(q.marks or 2), negative_marks=float(q.negative_marks or 0.5)))
        added += 1

    if added < min_needed:
        db.session.rollback()
        return None

    shown = added
    child.recalculate_totals()
    child.duration_seconds = max(60, shown * 60)
    rules = child.get_rules() or {}
    rules["db_test_source"] = {
        "test_type": scope, "subject": subject, "chapter": chapter, "topic": topic,
        "no_repeat_correct": True, "questions_per_attempt": shown, "pool_size": added,
    }
    rules["auto_generated"] = {"key": auto_key, "source": "db"}
    child.set_rules(rules)
    db.session.flush()
    return child


def _existing_children(exam_id: int) -> Dict[str, Exam]:
    out = {}
    for ex in Exam.query.filter_by(parent_exam_id=exam_id).all():
        r = ex.get_rules() or {}
        ag = r.get("auto_generated") or {}
        key = ag.get("key")
        if key:
            out[key] = ex
    return out


def generate_db_tests_for_exam(exam: Exam) -> Dict:
    if exam is None or exam.parent_exam_id is not None:
        return {"created": 0, "skipped": 0, "coming_soon": False, "tests": []}

    inv = _db_inventory()
    existing = _existing_children(exam.id)
    created = 0
    made: List[Dict] = []

    # Determine which subjects/chapters this exam's DB questions actually cover.
    # If exam already has directly-assigned questions, prefer those subjects.
    subjects: Dict[str, set] = defaultdict(set)
    for (s, c) in inv["chapters"]:
        subjects[s].add(c)

    def _try(scope, subject, chapter, topic, title):
        nonlocal created
        key = _auto_key(scope, subject or "", chapter or "", topic or "")
        if key in existing:
            return True
        ex = _build_test(exam, scope, subject, chapter, topic, key, title)
        if ex:
            existing[key] = ex
            created += 1
            made.append({"exam_id": ex.id, "title": ex.title, "scope": scope})
            return True
        return False

    for subject, chapters in subjects.items():
        chapter_covered = {}
        for chapter in sorted(chapters):
            chap_count = inv["chapters"].get((subject, chapter), 0)
            topics_here = [(t, cnt) for (s, c, t), cnt in inv["topics"].items() if s == subject and c == chapter]
            topic_made = False
            for (t, topic_cnt) in topics_here:
                if topic_cnt >= _MIN_TOPIC:
                    ok = _try("topic_wise", subject, chapter, t, f"{t} - Topic Test")
                    topic_made = topic_made or ok
            chapter_made = False
            if chap_count >= _MIN_CHAPTER:
                chapter_made = _try("chapter_wise", subject, chapter, None, f"{chapter} - Chapter Test")
            chapter_covered[chapter] = bool(chap_count > 0 and (chapter_made or topic_made))

        covered_count = sum(1 for ch in chapters if chapter_covered.get(ch))
        all_covered = len(chapters) > 0 and all(chapter_covered.get(ch) for ch in chapters)
        if all_covered and covered_count >= 2:
            _try("subject_wise", subject, None, None, f"{subject} - Subject Test")

    # NOTE: Full mock is intentionally DISABLED for now. Questions are still
    # being added subject-by-subject, so a real-exam-style full mock would be
    # incomplete (only 1-2 subjects present). We'll enable it once all subjects
    # (Quant + Reasoning + English + GA) have enough questions.
    # if len(subjects) >= 2:
    #     _try("full_mock", None, None, None, f"{exam.title} - Full Test")

    total_children = Exam.query.filter_by(parent_exam_id=exam.id).count()
    coming_soon = total_children == 0

    # Auto coming-soon: parent playable if it has child tests or its own questions.
    has_questions = (exam.total_questions or 0) > 0 or total_children > 0
    rules = exam.get_rules() or {}
    if has_questions:
        rules.pop("coming_soon", None)
    else:
        rules["coming_soon"] = {"active": True, "reason": "Questions abhi add nahi hue hain"}
    exam.set_rules(rules)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("generate_db_tests commit failed exam=%s", exam.id)
        return {"created": 0, "skipped": 0, "coming_soon": coming_soon, "tests": [], "error": "commit failed"}

    return {"created": created, "skipped": 0, "coming_soon": coming_soon,
            "subjects": list(subjects.keys()), "tests": made}


def generate_db_tests_all() -> Dict:
    total_created = 0
    total_exams = 0
    for exam in Exam.query.filter_by(parent_exam_id=None).all():
        if exam.status != "published":
            continue
        total_exams += 1
        res = generate_db_tests_for_exam(exam)
        total_created += res.get("created", 0)
    return {"exams_processed": total_exams, "tests_created": total_created}
