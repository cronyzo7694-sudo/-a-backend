"""
Exam-driven auto test generation.

Rules (agreed with product owner):
  * NO exam  -> NO test.
  * Exam created but its syllabus topics have NO questions in the file bank
    -> exam shows "Coming Soon" (a child placeholder), NO real test.
  * For each syllabus TOPIC that has questions in files -> a topic test.
  * For each syllabus CHAPTER that has questions -> a chapter test.
  * SUBJECT test only when ALL chapters of that subject (that the exam needs and
    the KB lists) have questions in files.
  * FULL test only when ALL subjects of the exam are fully covered.
  * Tests live INSIDE the exam (as child exams), never top-level.
  * Idempotent: re-running never duplicates (keyed by auto_key in rules).
  * Token friendly: answers come from files; AI only fills MISSING answers.

Public API:
  * generate_tests_for_exam(exam)      -> build/refresh tests for one exam
  * refresh_all_exams()                -> re-check every exam (after file upload)
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Dict, List, Optional, Set, Tuple

from app.extensions import db
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.question import Question, QuestionOption

# Per-exam locks so two concurrent runs (create's background thread + a file
# reload/refresh) can't both generate the same tests -> no duplicates.
_gen_locks: Dict[int, threading.Lock] = {}
_gen_locks_guard = threading.Lock()


def _lock_for(exam_id: int) -> threading.Lock:
    with _gen_locks_guard:
        lk = _gen_locks.get(exam_id)
        if lk is None:
            lk = threading.Lock()
            _gen_locks[exam_id] = lk
        return lk
from app.services import syllabus_kb as kb

logger = logging.getLogger("exam_os.services.auto_test")

PER_ATTEMPT = {
    "topic_wise": 12,
    "chapter_wise": 20,
    "subject_wise": 25,
    "full_mock": 100,
}
_MAX_POOL = 500
_MIN_TOPIC = 5       # minimum questions to bother making a topic test
_MIN_CHAPTER = 8


def _ai_keys() -> bool:
    return any(os.getenv(k) for k in
               ("GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"))


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def _auto_key(scope: str, subject: str, chapter: str, topic: str) -> str:
    return "|".join(x.strip().lower() for x in (scope, subject or "", chapter or "", topic or ""))


def _existing_children(exam_id: int) -> Dict[str, Exam]:
    """Map auto_key -> child Exam for this exam's already-created tests."""
    out: Dict[str, Exam] = {}
    for ex in Exam.query.filter_by(parent_exam_id=exam_id).all():
        try:
            rules = ex.get_rules() or {}
        except Exception:
            rules = {}
        ak = (rules.get("auto_generated") or {}).get("key")
        if ak:
            out[ak] = ex
    return out


# ---------------------------------------------------------------------------
# Syllabus resolution for an exam
# ---------------------------------------------------------------------------
def _parse_syllabus_terms(text: str) -> List[str]:
    if not text:
        return []
    parts = re.split(r"[,\n;:•\u2022]|\band\b|\|", text)
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if 2 <= len(p) <= 60:
            out.append(p)
    return out


def _syllabus_from_ai(exam_title: str) -> List[str]:
    if not _ai_keys():
        return []
    try:
        import json
        import requests
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            return []
        prompt = (
            f"List the exam '{exam_title[:120]}' syllabus as subjects and chapters. "
            f"Return ONLY JSON: {{\"items\":[\"subject or chapter name\", ...]}}. "
            f"Max 60 short names. Only JSON."
        )
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-1.5-flash:generateContent?key={key}")
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20)
        if r.status_code == 200:
            txt = (r.json().get("candidates", [{}])[0].get("content", {})
                   .get("parts", [{}])[0].get("text", ""))
            if "{" in txt:
                data = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
                return [str(t) for t in (data.get("items") or [])][:60]
    except Exception:
        logger.debug("AI syllabus research failed", exc_info=True)
    return []


def resolve_exam_syllabus(exam: Exam) -> Dict:
    """
    Return {"subjects": {subject: [chapters...]}, "source": "description|ai|title"}.
    Subjects/chapters are canonical (from syllabus_kb).
    """
    terms = _parse_syllabus_terms(exam.description or "")
    source = "description"
    if not terms:
        terms = _syllabus_from_ai(exam.title or "")
        source = "ai" if terms else "none"

    subjects: Dict[str, Set[str]] = {}

    # If a term is a subject -> include all its KB chapters.
    # If a term is a chapter -> include that chapter under its subject.
    for term in terms:
        subj = kb.canon_subject(term)
        if subj:
            subjects.setdefault(subj, set()).update(kb.chapters_for_subject(subj))
            continue
        chap = kb.canon_chapter(term)
        if chap:
            s = kb.subject_of_chapter(chap)
            if s:
                subjects.setdefault(s, set()).add(chap)

    # Fallback: if title mentions a known exam but we found nothing, use a broad
    # default (SSC-style 4 subjects) so at least structure exists.
    if not subjects:
        title = (exam.title or "").lower()
        if any(k in title for k in ("ssc", "chsl", "cgl", "mts", "clerk", "bank", "railway", "rrb")):
            for s in ("Reasoning", "Quantitative Aptitude", "English", "General Awareness"):
                subjects[s] = set(kb.chapters_for_subject(s))
            source = source if source != "none" else "title"

    return {"subjects": {s: sorted(c) for s, c in subjects.items()}, "source": source}


# ---------------------------------------------------------------------------
# File-bank inventory (what questions actually exist)
# ---------------------------------------------------------------------------
def _inventory() -> Dict:
    """
    Build canonical inventory from the file bank:
      topics:   {(subject, chapter, topic): count}
      chapters: {(subject, chapter): count}
      subjects: {subject: count}
    """
    from app.services.file_bank import FILE_QUESTIONS
    topics: Dict[Tuple[str, str, str], int] = {}
    chapters: Dict[Tuple[str, str], int] = {}
    subjects: Dict[str, int] = {}
    for q in FILE_QUESTIONS:
        subj = kb.canon_subject(q.get("subject")) or (q.get("subject") or "General")
        chap = kb.canon_chapter(q.get("chapter")) or (q.get("chapter") or "General")
        topic = q.get("topic") or chap
        topics[(subj, chap, topic)] = topics.get((subj, chap, topic), 0) + 1
        chapters[(subj, chap)] = chapters.get((subj, chap), 0) + 1
        subjects[subj] = subjects.get(subj, 0) + 1
    return {"topics": topics, "chapters": chapters, "subjects": subjects}


# ---------------------------------------------------------------------------
# Build one pool test (child of the exam)
# ---------------------------------------------------------------------------
def _build_test(exam: Exam, scope: str, subject: Optional[str],
                chapter: Optional[str], topic: Optional[str],
                auto_key: str, title: str) -> Optional[Exam]:
    from app.services.file_bank import filter_questions

    per_attempt = PER_ATTEMPT.get(scope, 20)
    pool_size = min(_MAX_POOL, per_attempt * 6)

    # NOTE: file bank stores original (non-canonical) names; match loosely.
    pool = filter_questions(subject=subject, chapter=chapter, topic=topic,
                            count=pool_size, shuffle=True)
    # canonical safety re-filter
    def _match(q):
        if subject and kb.canon_subject(q.get("subject")) != kb.canon_subject(subject):
            return False
        if chapter and kb.canon_chapter(q.get("chapter")) != kb.canon_chapter(chapter):
            return False
        if topic and (q.get("topic") or "").strip().lower() != topic.strip().lower():
            return False
        return True
    pool = [q for q in pool if _match(q)]
    if not pool:
        return None

    derive = None
    if _ai_keys():
        try:
            from app.services.knowledge_engine.free_ai_chain import derive_answer_with_ai as derive
        except Exception:
            derive = None

    min_needed = _MIN_TOPIC if scope == "topic_wise" else _MIN_CHAPTER
    answerable = [q for q in pool if q.get("correct_answer")]
    if len(answerable) < min_needed and not derive:
        return None

    # Auto MODE: if most questions carry a real exam tag (e.g. "SSC MTS 2024"),
    # this is a Previous Year Questions test -> mode "pyq"; else "mock".
    pyq_count = sum(1 for q in pool if q.get("exam_hint"))
    test_mode = "pyq" if pyq_count >= max(3, len(pool) // 2) else "mock"

    exam_obj = Exam(
        title=title[:255],
        description=f"Auto {scope.replace('_', ' ')} for {exam.title}",
        duration_seconds=per_attempt * 60,
        status="published",
        exam_mode=test_mode,
        default_marks=2,
        default_negative_marks=0.5,
        parent_exam_id=exam.id,
    )
    db.session.add(exam_obj)
    db.session.flush()

    section = ExamSection(exam_id=exam_obj.id, title=chapter or topic or subject or "General", order_index=0)
    db.session.add(section)
    db.session.flush()

    added = 0
    ai_used = 0
    for q in pool:
        options = q.get("options", [])[:4]
        correct = q.get("correct_answer")
        explanation = q.get("explanation") or ""
        if not correct and derive:
            ai = derive(q["question_text"], options)
            if ai:
                correct = ai["correct_answer"]
                explanation = explanation or ai.get("explanation", "")
                ai_used += 1
        if not correct:
            continue
        valid = {str(o.get("option_key", "")).upper() for o in options}
        if correct not in valid:
            continue
        qq = Question(
            question_text=q["question_text"][:2000],
            question_type="single_choice",
            difficulty=q.get("difficulty", "medium") if q.get("difficulty") in ("easy", "medium", "hard") else "medium",
            correct_answer=correct,
            explanation=explanation[:2000] if explanation else None,
            marks=2, negative_marks=0.5, is_active=True,
            tags=f"{subject or ''},{chapter or ''},{topic or ''}"[:512],
            source=q.get("source", "file_bank"),
        )
        db.session.add(qq)
        db.session.flush()
        for oi, opt in enumerate(options):
            db.session.add(QuestionOption(
                question_id=qq.id, option_key=opt.get("option_key", "A"),
                option_text=opt.get("option_text", "")[:500], order_index=oi))
        db.session.add(ExamQuestion(
            exam_id=exam_obj.id, section_id=section.id, question_id=qq.id,
            order_index=added, marks=2, negative_marks=0.5))
        added += 1

    if added < min_needed:
        db.session.rollback()
        return None

    shown = min(per_attempt, added)
    exam_obj.recalculate_totals()
    exam_obj.duration_seconds = max(60, shown * 60)
    rules = exam_obj.get_rules() or {}
    rules["file_bank_source"] = {
        "test_type": scope, "subject": subject, "chapter": chapter, "topic": topic,
        "no_repeat_correct": True, "questions_per_attempt": shown, "pool_size": added,
    }
    rules["auto_generated"] = {"key": auto_key, "ai_answers": ai_used}
    exam_obj.set_rules(rules)
    db.session.flush()
    return exam_obj


def _set_coming_soon(exam: Exam, on: bool, reason: str = "") -> None:
    """Mark the parent exam as 'coming soon' (no tests yet) in its rules."""
    try:
        rules = exam.get_rules() or {}
    except Exception:
        rules = {}
    rules["coming_soon"] = {"active": bool(on), "reason": reason}
    exam.set_rules(rules)


# ---------------------------------------------------------------------------
# Main: generate tests for ONE exam
# ---------------------------------------------------------------------------
def generate_tests_for_exam(exam: Exam) -> Dict:
    """Thread-safe wrapper: serialize generation per exam to prevent duplicate
    tests from concurrent runs (create bg-thread + file reload)."""
    if exam is None or exam.parent_exam_id is not None:
        return {"created": 0, "skipped": 0, "coming_soon": False, "tests": []}
    lock = _lock_for(exam.id)
    with lock:
        return _generate_tests_for_exam_locked(exam)


def _generate_tests_for_exam_locked(exam: Exam) -> Dict:
    """
    Create/refresh tests inside `exam` based on its syllabus + file bank.
    Returns a summary dict. Must be called under the per-exam lock.
    """
    if exam is None or exam.parent_exam_id is not None:
        # Only top-level exams get auto tests (children ARE the tests).
        return {"created": 0, "skipped": 0, "coming_soon": False, "tests": []}

    syl = resolve_exam_syllabus(exam)
    inv = _inventory()
    existing = _existing_children(exam.id)

    created = 0
    skipped = 0
    made: List[Dict] = []

    def _try(scope, subject, chapter, topic, title):
        nonlocal created, skipped
        key = _auto_key(scope, subject or "", chapter or "", topic or "")
        if key in existing:
            skipped += 1
            return True  # already exists -> counts as covered
        ex = _build_test(exam, scope, subject, chapter, topic, key, title)
        if ex:
            existing[key] = ex
            created += 1
            made.append({"exam_id": ex.id, "title": ex.title, "scope": scope})
            return True
        return False

    want_subjects = syl["subjects"]  # {subject: [chapters]}

    subject_fully_covered = {}
    for subject, chapters in want_subjects.items():
        chapter_covered = {}
        for chapter in chapters:
            # topics under this (subject, chapter) that have questions
            topics_here = [
                (t, cnt) for (s, c, t), cnt in inv["topics"].items()
                if s == subject and c == chapter
            ]
            chapter_qcount = inv["chapters"].get((subject, chapter), 0)

            # topic tests
            topic_made_any = False
            for (t, cnt) in topics_here:
                if cnt >= _MIN_TOPIC:
                    ok = _try("topic_wise", subject, chapter, t, f"{t} - Topic Test")
                    topic_made_any = topic_made_any or ok

            # chapter test (if the chapter has enough questions)
            chapter_made = False
            if chapter_qcount >= _MIN_CHAPTER:
                chapter_made = _try("chapter_wise", subject, chapter, None, f"{chapter} - Chapter Test")

            chapter_covered[chapter] = bool(chapter_qcount > 0 and (chapter_made or topic_made_any))

        # Subject test rule: bahut se chapters mil ke subject banta hai.
        # Make a subject test only when ALL syllabus chapters of this subject
        # are covered AND there are at least 2 covered chapters (ek akela chapter
        # -> sirf chapter test, subject test nahi).
        covered_count = sum(1 for ch in chapters if chapter_covered.get(ch, False))
        all_ch_covered = len(chapters) > 0 and all(chapter_covered.get(ch, False) for ch in chapters)
        make_subject = all_ch_covered and covered_count >= 2
        subject_fully_covered[subject] = make_subject
        if make_subject:
            _try("subject_wise", subject, None, None, f"{subject} - Subject Test")

    # Full test: saare subjects mil ke full paper. Only when EVERY exam subject
    # is fully covered AND the exam actually has 2+ subjects (warna ye chapter/
    # subject test jaisa hi ho jaayega).
    full_ready = (
        len(want_subjects) >= 2
        and all(subject_fully_covered.get(s, False) for s in want_subjects)
    )
    if full_ready:
        _try("full_mock", None, None, None, f"{exam.title} - Full Test")

    # Coming soon if NOTHING exists for this exam at all
    total_children = Exam.query.filter_by(parent_exam_id=exam.id).count()
    coming_soon = total_children == 0
    _set_coming_soon(exam, coming_soon,
                     "No questions in file bank for this exam's syllabus yet" if coming_soon else "")
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("generate_tests_for_exam commit failed exam=%s", exam.id)
        return {"created": 0, "skipped": skipped, "coming_soon": coming_soon,
                "tests": [], "error": "commit failed"}

    return {
        "created": created,
        "skipped": skipped,
        "coming_soon": coming_soon,
        "syllabus_source": syl["source"],
        "subjects": list(want_subjects.keys()),
        "tests": made,
    }


def refresh_all_exams() -> Dict:
    """Re-check every top-level exam (call after uploading/reloading files)."""
    total_created = 0
    total_coming = 0
    results = []
    for exam in Exam.query.filter_by(parent_exam_id=None).all():
        # skip the seeded demo exam? no—refresh everything top-level
        r = generate_tests_for_exam(exam)
        total_created += r.get("created", 0)
        if r.get("coming_soon"):
            total_coming += 1
        results.append({"exam_id": exam.id, "title": exam.title, **{k: r.get(k) for k in ("created", "coming_soon")}})
    return {"exams_checked": len(results), "tests_created": total_created,
            "coming_soon_exams": total_coming, "results": results}


def clear_exam_tests(exam_id: int) -> Dict:
    """
    Delete ONLY the child tests (and their questions/attempts) of one exam.
    The exam CARD itself is kept. Safe for Postgres (raw SQL, FK order).
    Returns {"tests_removed": n}.
    """
    from sqlalchemy import text, bindparam

    child_ids = [c.id for c in Exam.query.filter_by(parent_exam_id=exam_id).all()]
    if not child_ids:
        return {"tests_removed": 0}

    conn = db.session.connection()

    def _run(sql, ids):
        conn.execute(text(sql).bindparams(bindparam("ids", expanding=True)), {"ids": ids})

    # Collect question ids created for these child tests (to delete cleanly).
    q_rows = conn.execute(
        text("SELECT question_id FROM exam_questions WHERE exam_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": child_ids},
    ).fetchall()
    q_ids = [r[0] for r in q_rows if r[0] is not None]

    # Delete bottom-up (bypass ORM cascade -> no exam_id=NULL update crash).
    _run("DELETE FROM attempt_answers WHERE attempt_id IN "
         "(SELECT id FROM attempts WHERE exam_id IN :ids)", child_ids)
    _run("DELETE FROM attempt_answers WHERE exam_question_id IN "
         "(SELECT id FROM exam_questions WHERE exam_id IN :ids)", child_ids)
    _run("DELETE FROM attempts WHERE exam_id IN :ids", child_ids)
    _run("DELETE FROM exam_questions WHERE exam_id IN :ids", child_ids)
    _run("DELETE FROM exam_sections WHERE exam_id IN :ids", child_ids)
    _run("DELETE FROM exams WHERE id IN :ids", child_ids)
    # Remove the now-orphan questions that belonged only to these tests.
    if q_ids:
        _run("DELETE FROM question_options WHERE question_id IN :ids", q_ids)
        _run("DELETE FROM questions WHERE id IN :ids", q_ids)

    db.session.expire_all()
    return {"tests_removed": len(child_ids)}


def rebuild_exam_tests(exam_id: int) -> Dict:
    """
    Clear one exam's tests then regenerate them from the current file bank.
    The exam card is preserved. Ideal for 'refresh this exam after adding files'
    without touching any other exam.
    """
    exam = Exam.query.get(exam_id)
    if exam is None:
        return {"error": "exam not found"}
    lock = _lock_for(exam_id)
    with lock:
        cleared = clear_exam_tests(exam_id)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("rebuild_exam_tests clear-commit failed exam=%s", exam_id)
        exam = Exam.query.get(exam_id)
        summary = _generate_tests_for_exam_locked(exam)
    return {**cleared, **summary}


def rebuild_all_exams() -> Dict:
    """
    Safe alternative to factory reset: for EVERY exam, clear its tests and
    regenerate from files. Exam CARDS are kept (no data loss of exams). Users,
    subjects, and unrelated data are untouched.
    """
    results = []
    total_created = 0
    for exam in Exam.query.filter_by(parent_exam_id=None).all():
        r = rebuild_exam_tests(exam.id)
        total_created += r.get("created", 0)
        results.append({"exam_id": exam.id, "title": exam.title,
                        "tests_removed": r.get("tests_removed", 0),
                        "created": r.get("created", 0),
                        "coming_soon": r.get("coming_soon")})
    return {"exams_rebuilt": len(results), "tests_created": total_created, "results": results}
