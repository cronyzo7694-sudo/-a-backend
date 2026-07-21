# file_bank.py - Real Test Jaisa System - Chapter/Topic/Subject wise + Full Mock
# Reads REAL answers from "Answer Key" + "Sol." sections in each .txt file.
# Falls back to AI (Gemini/Groq/etc) ONLY when the file has no answer.
import os, re, random
from pathlib import Path
from collections import Counter

# ---------------------------------------------------------------------------
# Locate questions_data folder (checks several possible layouts)
# ---------------------------------------------------------------------------
POSSIBLE_BASES = [
    Path(__file__).parent.parent.parent / "questions_data",       # backend/questions_data (correct)
    Path(__file__).parent.parent / "questions_data",              # backend/app/questions_data
    Path(__file__).parent / "questions_data",                     # backend/app/services/questions_data
    Path(__file__).parent.parent.parent.parent / "questions_data" # root/questions_data
]

BASE = None
for b in POSSIBLE_BASES:
    if b.exists():
        BASE = b
        break
if BASE is None:
    BASE = Path(__file__).parent.parent.parent / "questions_data"

# ---------------------------------------------------------------------------
# Optional AI / knowledge-engine hooks (all optional, degrade gracefully)
# ---------------------------------------------------------------------------
try:
    from app.services.knowledge_engine.classifier import (
        classify_subject_chapter as local_classify,
        detect_bloom,
        detect_difficulty,
    )
    KNOWLEDGE_AVAILABLE = True
except Exception:
    KNOWLEDGE_AVAILABLE = False

    def local_classify(q, opts):
        ql = q.lower()
        if "analogy" in ql or ("::" in q):
            return {"subject": "Reasoning", "chapter": "Analogy", "topic": "Word Analogy",
                    "concepts": ["Analogy"], "pattern": "A:B::C:?", "question_family": "Analogy"}
        return {"subject": "Reasoning", "chapter": "General", "topic": "General",
                "concepts": [], "pattern": None, "question_family": "General"}

try:
    from app.services.knowledge_engine.free_ai_chain import classify_with_free_ai_chain
    FREE_AI_AVAILABLE = True
except Exception:
    FREE_AI_AVAILABLE = False


def _has_ai_keys():
    return any([
        os.getenv("GEMINI_API_KEY"),
        os.getenv("DEEPSEEK_API_KEY"),
        os.getenv("OPENROUTER_API_KEY"),
        os.getenv("GROQ_API_KEY"),
    ])


def smart_classify(question_text, options=None):
    """
    Classify a question into subject/chapter/topic.

    IMPORTANT (token saving): this runs for EVERY question at file-load time.
    So by default it uses ONLY the local heuristic (zero API cost).
    AI classification is opt-in via env FILE_BANK_AI_CLASSIFY=1 for people who
    want higher accuracy and don't mind spending free-tier quota once.
    """
    options = options or []
    use_ai = os.getenv("FILE_BANK_AI_CLASSIFY", "").lower() in ("1", "true", "yes", "on")
    if use_ai and FREE_AI_AVAILABLE and _has_ai_keys():
        try:
            ai_result = classify_with_free_ai_chain(question_text, local_classify)
            if ai_result and ai_result.get("subject"):
                local = local_classify(question_text, options)
                return {**local, **ai_result}
        except Exception:
            pass
    try:
        return local_classify(question_text, options)
    except Exception:
        return {"subject": "General", "chapter": "General", "topic": "General",
                "concepts": [], "pattern": None, "question_family": "General"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normspace(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _strip_junk(s):
    s = re.sub(r"www\.ssccglpinnacle\.com.*?(?:\n|$)", " ", s, flags=re.I)
    s = re.sub(r"Download\s+Pinnacle.*?(?:\n|$)", " ", s, flags=re.I)
    s = re.sub(r"Search\s+on\s+TG.*?(?:\n|$)", " ", s, flags=re.I)
    s = re.sub(r"Pinnacle\s+Day:.*?(?:\n|$)", " ", s, flags=re.I)
    s = re.sub(r"@apna_pdf", " ", s, flags=re.I)
    return s


def _clean_source_name(filename):
    """resoning analogy.txt -> Reasoning Analogy (best-effort human title)."""
    stem = Path(filename).stem
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"\bresoning\b", "Reasoning", stem, flags=re.I)
    return _normspace(stem).title()


# ---------------------------------------------------------------------------
# Answer-key + solution parsing (the important fix)
# ---------------------------------------------------------------------------
def _parse_answer_key(text):
    """
    Parse blocks like:
        Answer Key :-
        1.(a)  2.(b)  3.(a) ...
    Returns {qnum: 'A'} using the region AFTER 'Answer Key' and BEFORE 'Sol.'.
    """
    answers = {}
    ak_start = text.lower().find("answer key")
    sol_start = text.find("Sol.")
    if ak_start == -1:
        return answers
    region_end = sol_start if (sol_start != -1 and sol_start > ak_start) else len(text)
    region = text[ak_start:region_end]
    for m in re.finditer(r"(?<!\d)(\d{1,4})\s*\.\s*\(\s*([a-dA-D])\s*\)", region):
        answers[int(m.group(1))] = m.group(2).upper()
    return answers


def _parse_solutions(text):
    """
    Parse blocks like:
        Sol.1.(a)  Logic :- ....
        Sol.2.(b)  Logic:- ....
    Returns {qnum: {'answer': 'A', 'text': '...'}}.
    """
    solutions = {}
    sol_start = text.find("Sol.")
    if sol_start == -1:
        return solutions
    region = text[sol_start:]
    matches = list(re.finditer(r"Sol\.\s*(\d{1,4})\s*\.\s*\(\s*([a-dA-D])\s*\)", region))
    for i, m in enumerate(matches):
        qn = int(m.group(1))
        ans = m.group(2).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(region)
        body = _normspace(_strip_junk(region[start:end]))
        solutions[qn] = {"answer": ans, "text": body[:800]}
    return solutions


def _parse_question_blocks(body_region):
    """
    Split questions on 'Q.N.' markers and extract text + (a)-(d) options.
    Returns {qnum: {"question_text":..., "options":[{option_key,option_text}], "exam_hint":...}}
    """
    out = {}
    parts = re.split(r"\n\s*Q\.\s*(\d{1,4})\s*\.", "\n" + body_region)
    # parts = [pre, num1, block1, num2, block2, ...]
    for i in range(1, len(parts) - 1, 2):
        try:
            qn = int(parts[i])
        except ValueError:
            continue
        block = parts[i + 1]

        # options: (a)...(b)...(c)...(d)...
        opts = re.findall(
            r"\(\s*([a-d])\s*\)\s*(.+?)(?=\(\s*[a-d]\s*\)|Q\.\s*\d|$)",
            block, re.S | re.I,
        )
        if len(opts) < 2:
            continue

        # question text = everything before first "(a)"
        qsplit = re.split(r"\(\s*a\s*\)", block, flags=re.I)
        qtext = qsplit[0] if qsplit else block[:500]
        exam_hint_m = re.search(r"(SSC[^\n(]*\([^)]*\))", qtext)
        exam_hint = _normspace(exam_hint_m.group(1)) if exam_hint_m else None
        qtext = re.sub(r"SSC[^\n]*", "", qtext)     # drop exam-line noise from stem
        qtext = _normspace(_strip_junk(qtext))[:1200]
        if len(qtext) < 6:
            continue

        options = []
        for k, v in opts[:4]:
            v = _normspace(_strip_junk(v))[:300]
            if v:
                options.append({"option_key": k.upper(), "option_text": v})
        if len(options) < 2:
            continue

        out[qn] = {"question_text": qtext, "options": options, "exam_hint": exam_hint}
    return out


# ---------------------------------------------------------------------------
# Load every .txt into normalized question dicts (with REAL answers)
# ---------------------------------------------------------------------------
def load_questions_from_files():
    questions = []
    found_bases = [b for b in POSSIBLE_BASES if b.exists()]
    if BASE not in found_bases and BASE.exists():
        found_bases.append(BASE)
    if not found_bases:
        try:
            BASE.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return questions

    seen_files = set()
    for base_dir in found_bases:
        for txt_file in sorted(base_dir.glob("*.txt")):
            if txt_file.name in seen_files:
                continue
            seen_files.add(txt_file.name)
            try:
                text = txt_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            answer_key = _parse_answer_key(text)
            solutions = _parse_solutions(text)

            ak_start = text.lower().find("answer key")
            body_region = text[:ak_start] if ak_start != -1 else text
            parsed = _parse_question_blocks(body_region)

            source_title = _clean_source_name(txt_file.name)

            for qn, q in parsed.items():
                # RESOLVE ANSWER: answer key first, then solution, else None (AI later)
                correct = answer_key.get(qn) or solutions.get(qn, {}).get("answer")
                sol = solutions.get(qn, {})
                explanation = sol.get("text", "") if sol else ""

                qtext = q["question_text"]
                options = q["options"]

                try:
                    classification = smart_classify(qtext, options)
                except Exception:
                    classification = {"subject": source_title.split()[0] if source_title else "General",
                                      "chapter": "General", "topic": "General",
                                      "concepts": [], "pattern": None}

                try:
                    if KNOWLEDGE_AVAILABLE:
                        bloom = detect_bloom(qtext, [])
                        diff, score, mem, logic, calc, exp_time = detect_difficulty(qtext, bloom)
                    else:
                        diff, exp_time = "medium", 60
                except Exception:
                    diff, exp_time = "medium", 60

                questions.append({
                    "id": f"file_{txt_file.stem}_{qn}",
                    "qnum": qn,
                    "question_text": qtext,
                    "options": options,
                    "correct_answer": correct,          # <-- REAL answer (or None)
                    "explanation": explanation,         # <-- REAL solution text
                    "answer_source": ("file" if correct else "missing"),
                    "subject": classification.get("subject") or (source_title.split()[0] if source_title else "General"),
                    "chapter": classification.get("chapter") or "General",
                    "topic": classification.get("topic") or "General",
                    "subtopic": classification.get("subtopic"),
                    "concepts": classification.get("concepts", []),
                    "pattern": classification.get("pattern"),
                    "question_family": classification.get("question_family"),
                    "difficulty": diff if diff in ("easy", "medium", "hard") else "medium",
                    "expected_time": exp_time,
                    "source": txt_file.name,
                    "source_title": source_title,
                    "exam_hint": q.get("exam_hint"),
                })
    return questions


FILE_QUESTIONS = load_questions_from_files()


def reload_file_bank():
    """Re-scan questions_data (call after uploading a new file)."""
    global FILE_QUESTIONS
    FILE_QUESTIONS = load_questions_from_files()
    return len(FILE_QUESTIONS)


# ---------------------------------------------------------------------------
# Stats + filtering
# ---------------------------------------------------------------------------
def get_stats():
    if not FILE_QUESTIONS:
        return {"total": 0, "with_answer": 0, "without_answer": 0,
                "by_subject": {}, "by_chapter": {}, "by_topic": {}, "by_difficulty": {}}
    with_ans = sum(1 for q in FILE_QUESTIONS if q.get("correct_answer"))
    return {
        "total": len(FILE_QUESTIONS),
        "with_answer": with_ans,
        "without_answer": len(FILE_QUESTIONS) - with_ans,
        "by_subject": dict(Counter(q.get("subject", "Unknown") for q in FILE_QUESTIONS)),
        "by_chapter": dict(Counter(q.get("chapter", "Unknown") for q in FILE_QUESTIONS)),
        "by_topic": dict(Counter(q.get("topic", "Unknown") for q in FILE_QUESTIONS)),
        "by_difficulty": dict(Counter(q.get("difficulty", "medium") for q in FILE_QUESTIONS)),
        "sources": dict(Counter(q.get("source", "?") for q in FILE_QUESTIONS)),
        "sample_topics": list(Counter(q.get("topic", "Unknown") for q in FILE_QUESTIONS).keys())[:15],
    }


def filter_questions(subject=None, chapter=None, topic=None, difficulty=None,
                     count=20, exclude_ids=None, require_answer=False, shuffle=True):
    """
    Filter file questions.
      exclude_ids   : iterable of file-question ids to skip (no-repeat support)
      require_answer: only return questions that already have a resolved answer
    """
    exclude = set(exclude_ids or [])
    filtered = FILE_QUESTIONS

    if subject:
        s = subject.lower()
        filtered = [q for q in filtered if s in (q.get("subject", "").lower())]
    if chapter:
        c = chapter.lower()
        filtered = [q for q in filtered
                    if c in q.get("chapter", "").lower() or c in q.get("topic", "").lower()]
    if topic:
        t = topic.lower()
        filtered = [q for q in filtered
                    if t in q.get("topic", "").lower()
                    or t in (q.get("subtopic") or "").lower()
                    or t in (q.get("pattern") or "").lower()
                    or any(t in c.lower() for c in q.get("concepts", []))]
    if difficulty:
        filtered = [q for q in filtered if q.get("difficulty") == difficulty]
    if require_answer:
        filtered = [q for q in filtered if q.get("correct_answer")]
    if exclude:
        filtered = [q for q in filtered if q.get("id") not in exclude]

    if shuffle and len(filtered) > count:
        filtered = list(filtered)
        random.shuffle(filtered)
    return filtered[:count] if count else filtered
