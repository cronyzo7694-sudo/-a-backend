"""
Auto test generation (no button needed).

Two entry points:
  * generate_tests_for_bank()          -> called after files are (re)loaded
  * generate_tests_for_exam(exam)      -> called after an Exam is created

Design rules (agreed with product owner):
  * ONE file = ONE chapter. Subject/chapter come from the file name.
  * Only build tests for topics/chapters that ACTUALLY have questions in files.
    A syllabus topic with no questions is skipped silently.
  * If an exam's syllabus has NO matching questions at all, build NOTHING
    (user will press "Make test by AI" manually later).
  * Idempotent: re-running never creates duplicates (keyed by an auto_key
    stored in the exam rules).
  * Token friendly: answers come from files; AI is used ONLY as a fallback
    for missing answers, and syllabus AI-research runs only when there is no
    description and an AI key exists.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Set, Tuple

from app.extensions import db
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.question import Question, QuestionOption

logger = logging.getLogger("exam_os.services.auto_test")

# Real-paper "questions shown per attempt" by scope
PER_ATTEMPT = {
    "topic_wise": 12,
    "chapter_wise": 20,
    "subject_wise": 25,
    "full_mock": 100,
}
_MAX_POOL = 500
_MIN_QUESTIONS_FOR_TOPIC = 5      # don't make a test with fewer than this
_MIN_QUESTIONS_FOR_CHAPTER = 8


def _ai_keys() -> bool:
    return any(os.getenv(k) for k in
               ("GEMINI_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"))


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------
def _existing_auto_keys(parent_id: Optional[int] = None) -> Set[str]:
    keys: Set[str] = set()
    q = Exam.query
    if parent_id is not None:
        q = q.filter_by(parent_exam_id=parent_id)
    for ex in q.all():
        try:
            rules = ex.get_rules() or {}
            ak = (rules.get("auto_generated") or {}).get("key")
            if ak:
                keys.add(ak)
        except Exception:
            continue
    return keys


def _make_auto_key(scope: str, subject: str, chapter: str, topic: str, parent_id: Optional[int]) -> str:
    parts = [str(parent_id or "root"), scope, subject or "", chapter or "", topic or ""]
    return "|".join(p.strip().lower() for p in parts)


# ---------------------------------------------------------------------------
# Pool test builder (shared, self-contained)
# ---------------------------------------------------------------------------
def build_pool_test(
    *,
    scope: str,
    subject: Optional[str] = None,
    chapter: Optional[str] = None,
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    parent_exam_id: Optional[int] = None,
    auto_key: Optional[str] = None,
    use_ai_for_missing: bool = True,
    commit: bool = True,
) -> Optional[Exam]:
    """
    Create ONE shared pool exam from the file bank. Returns the Exam or None
    if not enough answerable questions were found. Never guesses answers.
    """
    from app.services.file_bank import filter_questions

    per_attempt = PER_ATTEMPT.get(scope, 20)
    pool_size = min(_MAX_POOL, per_attempt * 6)

    pool = filter_questions(
        subject=subject, chapter=chapter, topic=topic,
        difficulty=difficulty, count=pool_size, shuffle=True,
    )
    if not pool:
        return None

    # AI fallback for missing answers (optional)
    derive = None
    if use_ai_for_missing and _ai_keys():
        try:
            from app.services.knowledge_engine.free_ai_chain import derive_answer_with_ai as derive
        except Exception:
            derive = None

    min_needed = _MIN_QUESTIONS_FOR_TOPIC if scope == "topic_wise" else _MIN_QUESTIONS_FOR_CHAPTER
    if scope in ("subject_wise", "full_mock"):
        min_needed = _MIN_QUESTIONS_FOR_CHAPTER

    # First, count how many are answerable WITHOUT spending AI, to bail early.
    answerable = [fq for fq in pool if fq.get("correct_answer")]
    if len(answerable) < min_needed and not derive:
        return None

    label = topic or chapter or subject or "Practice"
    scope_name = {
        "topic_wise": "Topic Test",
        "chapter_wise": "Chapter Test",
        "subject_wise": "Subject Test",
        "full_mock": "Full Mock",
    }.get(scope, "Test")
    if not title:
        title = f"{label} - {scope_name}"
    if not description:
        description = f"Auto-generated {scope.replace('_', ' ')} from file bank: {label}"

    exam = Exam(
        title=title[:255],
        description=description[:1000],
        duration_seconds=per_attempt * 60,
        status="published",
        exam_mode="mock",
        default_marks=2,
        default_negative_marks=0.5,
        parent_exam_id=parent_exam_id,
    )
    db.session.add(exam)
    db.session.flush()

    section = ExamSection(exam_id=exam.id, title=chapter or topic or subject or "General", order_index=0)
    db.session.add(section)
    db.session.flush()

    added = 0
    ai_derived = 0
    for fq in pool:
        options = fq.get("options", [])[:4]
        correct = fq.get("correct_answer")
        explanation = fq.get("explanation") or ""
        answer_source = fq.get("answer_source", "file")

        if not correct and derive:
            ai = derive(fq["question_text"], options)
            if ai:
                correct = ai["correct_answer"]
                explanation = explanation or ai.get("explanation", "")
                answer_source = ai.get("source", "ai")
                ai_derived += 1

        if not correct:
            continue
        valid = {str(o.get("option_key", "")).upper() for o in options}
        if correct not in valid:
            continue

        q = Question(
            question_text=fq["question_text"][:2000],
            question_type="single_choice",
            difficulty=fq.get("difficulty", "medium") if fq.get("difficulty") in ("easy", "medium", "hard") else "medium",
            correct_answer=correct,
            explanation=explanation[:2000] if explanation else None,
            marks=2, negative_marks=0.5, is_active=True,
            tags=f"{fq.get('subject','')},{fq.get('chapter','')},{fq.get('topic','')},src:{answer_source}"[:512],
            source=fq.get("source", "file_bank"),
        )
        db.session.add(q)
        db.session.flush()
        for oi, opt in enumerate(options):
            db.session.add(QuestionOption(
                question_id=q.id, option_key=opt.get("option_key", "A"),
                option_text=opt.get("option_text", "")[:500], order_index=oi,
            ))
        db.session.add(ExamQuestion(
            exam_id=exam.id, section_id=section.id, question_id=q.id,
            order_index=added, marks=2, negative_marks=0.5,
        ))
        added += 1

    if added < min_needed:
        # Not enough -> roll back this exam so we don't leave a stub
        db.session.rollback()
        return None

    shown = min(per_attempt, added)
    exam.recalculate_totals()
    exam.duration_seconds = max(60, shown * 60)
    rules = exam.get_rules() or {}
    rules["file_bank_source"] = {
        "test_type": scope, "subject": subject, "chapter": chapter, "topic": topic,
        "difficulty": difficulty, "no_repeat_correct": True,
        "questions_per_attempt": shown, "pool_size": added,
    }
    if auto_key:
        rules["auto_generated"] = {"key": auto_key, "ai_answers": ai_derived}
    exam.set_rules(rules)

    if commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("build_pool_test commit failed")
            return None
    return exam


# ---------------------------------------------------------------------------
# Auto-generate from the whole file bank (called after file reload)
# ---------------------------------------------------------------------------
def generate_tests_for_bank(parent_exam_id: Optional[int] = None) -> Dict:
    """
    For every subject / chapter / topic that has enough questions in files,
    create a shared pool test (if it doesn't already exist).
    """
    from app.services.file_bank import FILE_QUESTIONS
    if not FILE_QUESTIONS:
        return {"created": 0, "skipped": 0, "tests": []}

    existing = _existing_auto_keys(parent_exam_id)
    created, skipped = 0, 0
    made: List[Dict] = []

    # Build the (subject, chapter, topic) inventory from files
    subjects: Set[str] = set()
    chapters: Set[Tuple[str, str]] = set()
    topics: Set[Tuple[str, str, str]] = set()
    for q in FILE_QUESTIONS:
        s = q.get("subject") or "General"
        c = q.get("chapter") or "General"
        t = q.get("topic") or "General"
        subjects.add(s)
        chapters.add((s, c))
        topics.add((s, c, t))

    def _try(scope, subject, chapter, topic):
        nonlocal created, skipped
        key = _make_auto_key(scope, subject or "", chapter or "", topic or "", parent_exam_id)
        if key in existing:
            skipped += 1
            return
        ex = build_pool_test(
            scope=scope, subject=subject, chapter=chapter, topic=topic,
            parent_exam_id=parent_exam_id, auto_key=key, commit=True,
        )
        if ex:
            existing.add(key)
            created += 1
            made.append({"exam_id": ex.id, "title": ex.title, "scope": scope})
        else:
            skipped += 1

    # topic-wise (most specific), then chapter, then subject, then full mock
    for (s, c, t) in sorted(topics):
        _try("topic_wise", s, c, t)
    for (s, c) in sorted(chapters):
        _try("chapter_wise", s, c, None)
    for s in sorted(subjects):
        _try("subject_wise", s, None, None)
    # one full mock across everything
    _try("full_mock", None, None, None)

    return {"created": created, "skipped": skipped, "tests": made}


# ---------------------------------------------------------------------------
# Auto-generate for a specific exam (called after Exam is created)
# ---------------------------------------------------------------------------
def _syllabus_from_description(text: str) -> List[str]:
    """Extract candidate chapter/topic names from a free-text description."""
    if not text:
        return []
    import re
    # split on commas / newlines / semicolons / bullets
    parts = re.split(r"[,\n;•\-\u2022]| and ", text)
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if 2 <= len(p) <= 60:
            out.append(p)
    return out


def _syllabus_from_ai(exam_title: str) -> List[str]:
    """Best-effort AI syllabus research. Returns chapter/topic name list or []."""
    if not _ai_keys():
        return []
    try:
        import json
        import requests
        key = os.getenv("GEMINI_API_KEY")
        if key:
            prompt = (
                f"List the main topics/chapters in the syllabus of the exam "
                f"'{exam_title[:120]}'. Return ONLY JSON: {{\"topics\":[\"...\"]}}. "
                f"Max 40 short topic names. Only JSON."
            )
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"gemini-1.5-flash:generateContent?key={key}")
            r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20)
            if r.status_code == 200:
                txt = (r.json().get("candidates", [{}])[0].get("content", {})
                       .get("parts", [{}])[0].get("text", ""))
                if "{" in txt:
                    data = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
                    return [str(t) for t in (data.get("topics") or [])][:40]
    except Exception:
        logger.debug("AI syllabus research failed", exc_info=True)
    return []


def generate_tests_for_exam(exam: Exam) -> Dict:
    """
    Create child tests under `exam` for every syllabus topic that has questions
    in the file bank. Topics without questions are skipped silently. If nothing
    matches, no tests are created (user can 'Make test by AI' manually).
    """
    from app.services.file_bank import FILE_QUESTIONS
    if not FILE_QUESTIONS:
        return {"created": 0, "skipped": 0, "tests": [], "reason": "no file questions"}

    # 1) Determine syllabus terms: description first, else AI research
    terms = _syllabus_from_description(exam.description or "")
    used_ai = False
    if not terms:
        terms = _syllabus_from_ai(exam.title or "")
        used_ai = bool(terms)

    # 2) Build available inventory from files
    inv_topics = {}
    inv_chapters = {}
    inv_subjects = set()
    for q in FILE_QUESTIONS:
        s = q.get("subject") or "General"
        c = q.get("chapter") or "General"
        t = q.get("topic") or "General"
        inv_subjects.add(s)
        inv_chapters.setdefault((s, c), 0)
        inv_chapters[(s, c)] += 1
        inv_topics.setdefault((s, c, t), 0)
        inv_topics[(s, c, t)] += 1

    existing = _existing_auto_keys(exam.id)
    created, skipped = 0, 0
    made: List[Dict] = []

    def _match(term: str) -> bool:
        return True  # term matching handled per-candidate below

    def _try(scope, s, c, t, title=None):
        nonlocal created, skipped
        key = _make_auto_key(scope, s or "", c or "", t or "", exam.id)
        if key in existing:
            skipped += 1
            return
        ex = build_pool_test(
            scope=scope, subject=s, chapter=c, topic=t,
            parent_exam_id=exam.id, auto_key=key,
            title=title, commit=True,
        )
        if ex:
            existing.add(key)
            created += 1
            made.append({"exam_id": ex.id, "title": ex.title, "scope": scope})
        else:
            skipped += 1

    # 3) If we have syllabus terms, only build tests whose chapter/topic/subject
    #    name matches a term. Otherwise (no desc, no AI) build for everything
    #    available in files (still only what exists -> skip_silent honored).
    def term_hits(name: str) -> bool:
        if not terms:
            return True
        n = (name or "").lower()
        for term in terms:
            tl = term.lower()
            if tl and (tl in n or n in tl):
                return True
        return False

    for (s, c, t), cnt in sorted(inv_topics.items()):
        if term_hits(t) or term_hits(c) or term_hits(s):
            _try("topic_wise", s, c, t)
    for (s, c), cnt in sorted(inv_chapters.items()):
        if term_hits(c) or term_hits(s):
            _try("chapter_wise", s, c, None)
    for s in sorted(inv_subjects):
        if term_hits(s):
            _try("subject_wise", s, None, None)

    # Full mock only if at least something matched
    if made:
        _try("full_mock", None, None, None, title=f"{exam.title} - Full Mock")

    return {"created": created, "skipped": skipped, "tests": made,
            "syllabus_source": ("ai" if used_ai else ("description" if terms else "all_files")),
            "syllabus_terms": terms[:40]}
