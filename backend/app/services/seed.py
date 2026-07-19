"""Idempotent demo dataset bootstrap for Exam OS.

Called from the application factory on startup. Safe to invoke multiple times:
if the canonical admin account already exists, the function returns immediately
without mutating production data.

Seeds:
    * admin@examos.local / admin123
    * student@examos.local / student123
    * 4 subjects, chapters, mixed question types, 1 published mock exam

Public contract (stable)::

    seed_database() -> None
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Sequence

from app.extensions import db
from app.models.chapter import Chapter
from app.models.exam import Exam, ExamQuestion, ExamSection
from app.models.question import Question, QuestionOption
from app.models.subject import Subject
from app.models.user import User

logger = logging.getLogger("exam_os.services.seed")

# Canonical bootstrap identity — used as the idempotency sentinel
_ADMIN_EMAIL = "admin@examos.local"
_STUDENT_EMAIL = "student@examos.local"
_ADMIN_PASSWORD = "admin123"
_STUDENT_PASSWORD = "student123"


def _add_mcq(
    *,
    admin_id: int,
    subject: Subject,
    chapter: Optional[Chapter],
    text: str,
    options: Sequence[str],
    correct: Any,
    explanation: str,
    difficulty: str = "medium",
    marks: float = 2.0,
    neg: float = 0.5,
    qtype: str = "single_choice",
    html: Optional[str] = None,
    image: Optional[str] = None,
    paragraph: Optional[str] = None,
    tags: str = "demo,seed",
) -> Question:
    """Create a choice-style question with A/B/C/… options and flush."""
    if isinstance(correct, (list, tuple)):
        correct_answer = json.dumps(list(correct))
    else:
        correct_answer = str(correct)

    q = Question(
        subject_id=subject.id,
        chapter_id=chapter.id if chapter is not None else None,
        question_type=qtype,
        difficulty=difficulty,
        question_text=text,
        question_html=html,
        explanation=explanation,
        correct_answer=correct_answer,
        marks=marks,
        negative_marks=neg,
        image_url=image,
        paragraph_text=paragraph,
        created_by=admin_id,
        tags=tags,
        is_active=True,
        language="en",
    )
    db.session.add(q)
    db.session.flush()

    for i, opt_text in enumerate(options):
        if i >= 26:
            break
        key = chr(65 + i)
        db.session.add(
            QuestionOption(
                question_id=q.id,
                option_key=key,
                option_text=str(opt_text),
                order_index=i,
            )
        )
    return q


def seed_database() -> None:
    """
    Populate an empty database with demo CBT content.

    Idempotent: returns without writes when ``admin@examos.local`` exists.
    On failure rolls back the session so a partial seed cannot leave the
    schema half-populated for the next boot.
    """
    try:
        if User.query.filter_by(email=_ADMIN_EMAIL).first() is not None:
            logger.debug("seed_database skipped — demo admin already present")
            return
    except Exception:
        # Table may not exist yet in exotic bootstrap orders; let create_all handle
        logger.exception("seed_database pre-check failed")
        raise

    logger.info("Seeding Exam OS demo dataset")

    try:
        admin = User(
            email=_ADMIN_EMAIL,
            full_name="Admin User",
            role="admin",
            is_active=True,
        )
        admin.set_password(_ADMIN_PASSWORD)
        student = User(
            email=_STUDENT_EMAIL,
            full_name="Demo Student",
            role="student",
            is_active=True,
        )
        student.set_password(_STUDENT_PASSWORD)
        db.session.add_all([admin, student])
        db.session.flush()

        # Subjects
        subjects_data = [
            ("Quantitative Aptitude", "QA", "#2563eb", "calculator"),
            ("Reasoning Ability", "RA", "#16a34a", "brain"),
            ("English Language", "EL", "#f59e0b", "book"),
            ("General Awareness", "GA", "#ef4444", "globe"),
        ]
        subjects: List[Subject] = []
        for i, (name, code, color, icon) in enumerate(subjects_data):
            s = Subject(
                name=name,
                code=code,
                color=color,
                icon=icon,
                order_index=i,
                description=f"{name} for competitive exams",
                is_active=True,
            )
            db.session.add(s)
            subjects.append(s)
        db.session.flush()

        # Chapters
        chapters_map = {
            0: ["Number System", "Percentage", "Profit & Loss", "Time & Work", "Algebra"],
            1: ["Analogy", "Series", "Coding-Decoding", "Blood Relations", "Syllogism"],
            2: ["Reading Comprehension", "Error Spotting", "Fill in the Blanks", "Vocabulary"],
            3: ["Indian History", "Geography", "Polity", "Current Affairs", "Science"],
        }
        chapters: List[Chapter] = []
        for si, names in chapters_map.items():
            for ci, cname in enumerate(names):
                ch = Chapter(
                    subject_id=subjects[si].id,
                    name=cname,
                    order_index=ci,
                    is_active=True,
                )
                db.session.add(ch)
                chapters.append(ch)
        db.session.flush()

        qa, ra, el, ga = subjects
        ch_num = chapters[0]
        ch_pct = chapters[1]
        ch_analogy = chapters[5]
        ch_series = chapters[6]
        ch_rc = chapters[10]
        ch_hist = chapters[14]
        ch_algebra = chapters[4]

        sample_questions: List[Question] = []
        admin_id = admin.id

        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=qa,
                chapter=ch_num,
                text="What is the LCM of 12 and 18?",
                options=["24", "36", "48", "54"],
                correct="B",
                explanation="LCM(12,18) = 2² × 3² = 36",
                difficulty="easy",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=qa,
                chapter=ch_pct,
                text="If 20% of a number is 50, what is the number?",
                options=["200", "250", "300", "150"],
                correct="B",
                explanation="0.2x = 50 ⇒ x = 250",
                difficulty="easy",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=qa,
                chapter=ch_pct,
                text="A shopkeeper marks goods 40% above cost and gives 10% discount. Find profit %.",
                options=["26%", "30%", "24%", "28%"],
                correct="A",
                explanation="SP = 1.4C × 0.9 = 1.26C ⇒ 26% profit",
                difficulty="medium",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=ra,
                chapter=ch_analogy,
                text="Book : Reading :: Fork : ?",
                options=["Drawing", "Writing", "Eating", "Stirring"],
                correct="C",
                explanation="Book is used for reading; fork is used for eating.",
                difficulty="easy",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=ra,
                chapter=ch_series,
                text="Find the next number: 2, 6, 12, 20, 30, ?",
                options=["40", "42", "44", "46"],
                correct="B",
                explanation="Differences: +4,+6,+8,+10,+12 ⇒ 30+12=42",
                difficulty="medium",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=ra,
                chapter=ch_series,
                text="In a certain code, CAT is written as 3120. How is DOG written?",
                options=["4157", "4715", "4158", "4517"],
                correct="A",
                explanation="Demo coding pattern — D=4, O=15, G=7 → 4157",
                difficulty="hard",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=el,
                chapter=ch_rc,
                text="Choose the correct synonym of 'Abundant'.",
                options=["Scarce", "Plentiful", "Tiny", "Rare"],
                correct="B",
                explanation="Abundant means existing in large quantities; synonym is plentiful.",
                difficulty="easy",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=el,
                chapter=ch_rc,
                text="Identify the error: 'She don't like coffee.'",
                options=["She", "don't", "like", "No error"],
                correct="B",
                explanation="Third person singular requires 'doesn't'.",
                difficulty="easy",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=ga,
                chapter=ch_hist,
                text="Who was the first President of India?",
                options=[
                    "Jawaharlal Nehru",
                    "Dr. Rajendra Prasad",
                    "Sardar Patel",
                    "Dr. S. Radhakrishnan",
                ],
                correct="B",
                explanation="Dr. Rajendra Prasad was the first President of India (1950–1962).",
                difficulty="easy",
            )
        )
        sample_questions.append(
            _add_mcq(
                admin_id=admin_id,
                subject=ga,
                chapter=ch_hist,
                text="The Constitution of India came into force on:",
                options=[
                    "15 August 1947",
                    "26 January 1950",
                    "26 November 1949",
                    "2 October 1947",
                ],
                correct="B",
                explanation=(
                    "Constitution was adopted on 26 Nov 1949 and came into force on 26 Jan 1950."
                ),
                difficulty="medium",
            )
        )

        # Integer type
        q_int = Question(
            subject_id=qa.id,
            chapter_id=ch_num.id,
            question_type="integer",
            difficulty="medium",
            question_text="How many prime numbers are there between 1 and 20?",
            explanation="Primes: 2,3,5,7,11,13,17,19 → 8 primes",
            correct_answer="8",
            marks=2.0,
            negative_marks=0.0,
            created_by=admin_id,
            tags="demo,integer",
            is_active=True,
            language="en",
        )
        db.session.add(q_int)
        db.session.flush()
        sample_questions.append(q_int)

        # Math / MathJax
        q_math = Question(
            subject_id=qa.id,
            chapter_id=ch_algebra.id,
            question_type="math",
            difficulty="medium",
            question_text=r"If \( x^2 - 5x + 6 = 0 \), what is the sum of roots?",
            question_html=r"If \( x^2 - 5x + 6 = 0 \), what is the sum of roots?",
            explanation=r"Sum of roots = \( -b/a = 5 \)",
            explanation_html=r"For \( ax^2+bx+c=0 \), sum of roots \( = -b/a = 5 \).",
            correct_answer="B",
            marks=2.0,
            negative_marks=0.5,
            created_by=admin_id,
            tags="demo,math,algebra",
            is_active=True,
            language="en",
        )
        db.session.add(q_math)
        db.session.flush()
        for i, t in enumerate(["4", "5", "6", "11"]):
            db.session.add(
                QuestionOption(
                    question_id=q_math.id,
                    option_key=chr(65 + i),
                    option_text=t,
                    order_index=i,
                )
            )
        sample_questions.append(q_math)

        # Paragraph based
        para = (
            "Read the passage: The Indus Valley Civilization was one of the world's "
            "earliest urban cultures. It flourished around the Indus River basin. "
            "Major cities included Harappa and Mohenjo-daro. The people were skilled "
            "in town planning and drainage systems."
        )
        q_para = _add_mcq(
            admin_id=admin_id,
            subject=ga,
            chapter=ch_hist,
            text=(
                "According to the passage, which were major cities of the "
                "Indus Valley Civilization?"
            ),
            options=[
                "Delhi and Agra",
                "Harappa and Mohenjo-daro",
                "Pataliputra and Taxila",
                "Varanasi and Mathura",
            ],
            correct="B",
            explanation="The passage explicitly mentions Harappa and Mohenjo-daro.",
            difficulty="easy",
            paragraph=para,
            qtype="paragraph",
        )
        sample_questions.append(q_para)

        # Image type (placeholder path — works without a real asset)
        q_img = _add_mcq(
            admin_id=admin_id,
            subject=ra,
            chapter=ch_analogy,
            text=(
                "Based on the figure pattern (described): Triangle → Square → Pentagon. "
                "Next shape sides?"
            ),
            options=["5", "6", "7", "8"],
            correct="B",
            explanation="Sides increase by 1 each time: 3,4,5 → next is hexagon with 6 sides.",
            difficulty="medium",
            qtype="image",
            image="/placeholder-figure.svg",
        )
        sample_questions.append(q_img)

        # Multiple choice
        q_multi = Question(
            subject_id=qa.id,
            chapter_id=ch_num.id,
            question_type="multiple_choice",
            difficulty="hard",
            question_text="Which of the following are prime numbers? (Select all that apply)",
            explanation="2, 3, 5, 7 are prime; 4, 9, 1 are not.",
            correct_answer=json.dumps(["A", "C"]),
            marks=2.0,
            negative_marks=0.5,
            created_by=admin_id,
            tags="demo,multi",
            is_active=True,
            language="en",
        )
        db.session.add(q_multi)
        db.session.flush()
        for i, t in enumerate(["2 and 3", "4 and 9", "5 and 7", "1 and 9"]):
            db.session.add(
                QuestionOption(
                    question_id=q_multi.id,
                    option_key=chr(65 + i),
                    option_text=t,
                    order_index=i,
                )
            )
        sample_questions.append(q_multi)

        db.session.flush()

        # Demo exam — SSC-style multi-section mock
        exam = Exam(
            title="SSC CHSL Style Mock Test — Demo",
            description="A complete demo mock covering Quant, Reasoning, English and GA.",
            instructions=(
                "1. Total duration is 30 minutes.\n"
                "2. Each question carries marks as shown.\n"
                "3. There is negative marking for wrong answers where applicable.\n"
                "4. You can mark questions for review.\n"
                "5. Click Submit when finished. Auto-submit on timer expiry."
            ),
            exam_mode="mock",
            status="published",
            duration_seconds=30 * 60,
            strict_sections=False,
            default_marks=2.0,
            default_negative_marks=0.5,
            shuffle_questions=False,
            shuffle_options=False,
            require_fullscreen=False,
            max_tab_switches=10,
            show_result_immediately=True,
            created_by=admin_id,
        )
        db.session.add(exam)
        db.session.flush()

        section_defs = [
            (
                "Quantitative Aptitude",
                qa.id,
                sample_questions[0:3] + [q_int, q_math, q_multi],
            ),
            (
                "Reasoning Ability",
                ra.id,
                sample_questions[3:6] + [q_img],
            ),
            (
                "English Language",
                el.id,
                sample_questions[6:8],
            ),
            (
                "General Awareness",
                ga.id,
                sample_questions[8:10] + [q_para],
            ),
        ]

        order = 0
        for si, (stitle, sid, qs) in enumerate(section_defs):
            sec = ExamSection(
                exam_id=exam.id,
                title=stitle,
                order_index=si,
                duration_seconds=None,
                subject_id=sid,
            )
            db.session.add(sec)
            db.session.flush()
            for q in qs:
                db.session.add(
                    ExamQuestion(
                        exam_id=exam.id,
                        section_id=sec.id,
                        question_id=q.id,
                        order_index=order,
                        marks=float(q.marks if q.marks is not None else 2.0),
                        negative_marks=float(
                            q.negative_marks if q.negative_marks is not None else 0.0
                        ),
                    )
                )
                order += 1

        exam.recalculate_totals()
        # Default configuration pack for the demo mock (rule-engine driven)
        try:
            from app.services.rule_engine import apply_column_sync_to_rules, merge_exam_rules

            exam.set_rules(
                apply_column_sync_to_rules(
                    exam,
                    merge_exam_rules({
                        "timing": {"auto_submit_on_expiry": True, "allow_pause": False},
                        "navigation": {"resume_allowed": True, "allow_mark_for_review": True},
                        "security": {"detect_tab_switch": True, "max_tab_switches": 10},
                        "aids": {"calculator_allowed": False, "rough_sheet_allowed": True},
                        "marking": {"negative_marking": True},
                        "result": {
                            "show_immediate": True,
                            "show_time_analysis": True,
                            "show_percentile": True,
                        },
                    }),
                )
            )
        except Exception:
            logger.exception("demo exam rules seed skipped")

        db.session.commit()

        logger.info(
            "Database seeded: %s / %s , %s / %s (exam_id=%s questions=%s)",
            _ADMIN_EMAIL,
            _ADMIN_PASSWORD,
            _STUDENT_EMAIL,
            _STUDENT_PASSWORD,
            exam.id,
            exam.total_questions,
        )
        # Keep stdout breadcrumb for operators running python app.py interactively
        print(
            "Database seeded: "
            f"{_ADMIN_EMAIL} / {_ADMIN_PASSWORD} , "
            f"{_STUDENT_EMAIL} / {_STUDENT_PASSWORD}"
        )

    except Exception:
        db.session.rollback()
        logger.exception("seed_database failed — transaction rolled back")
        raise


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - EXAM_OS_SEED_PASSWORD env override for demo credentials in shared envs
# - Fixture JSON files instead of inline content for localization packs
# - Optional seed of SSC rule presets into exam.rules_json
# --------------------------------------------
