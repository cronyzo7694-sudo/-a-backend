"""
Layer 4: CLASSIFIER - Intelligent subject/chapter/topic detection
Like human teacher, not keyword matcher. Robust for noisy OCR.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .constants import SUBJECT_KEYWORDS, CHAPTER_MAP, BLOOM_KEYWORDS, EXAM_PATTERNS


def detect_bloom(question: str, options: List[Dict]) -> str:
    ql = (question + " " + " ".join([o.get("option_text","") for o in options])).lower()
    for level, keywords in BLOOM_KEYWORDS.items():
        if any(w.lower() in ql for w in keywords):
            return level
    if re.search(r'\d+.*[+\-*/=].*\d+', question):
        return "apply"
    if ":" in question and "::" in question:
        return "analyze"
    return "understand"


def detect_difficulty(question: str, bloom: str) -> Tuple[str, float, int, int, int, int]:
    """
    Returns difficulty, score 0-1, memory, logic, calc, expected_time_seconds
    """
    length = len(question)
    has_numbers = bool(re.search(r'\d+', question))
    has_complex = bool(re.search(r'[+\-*/^%]', question))

    memory, logic, calc = 3, 3, 2
    expected_time = 60

    if bloom == "remember":
        memory, logic, calc = 5, 1, 1
        difficulty = "easy"
        expected_time = 30
    elif bloom == "apply" and has_numbers and has_complex:
        calc, logic = 4, 4
        difficulty = "medium"
        expected_time = 90
    elif bloom == "analyze":
        logic, memory = 5, 2
        difficulty = "hard"
        expected_time = 120
    elif bloom == "evaluate":
        logic = 5
        difficulty = "hard"
        expected_time = 150
    else:
        difficulty = "medium"

    if length > 500:
        expected_time += 30
        if difficulty == "medium":
            difficulty = "hard"

    # Specific patterns: 132 : 156 :: 462 : ? is easy number analogy
    if re.search(r'\d+\s*:\s*\d+\s*::\s*\d+\s*:\s*\?', question):
        difficulty = "easy"
        expected_time = 45
        logic = 4

    # Pressure : Pascal is memory
    if re.search(r'[A-Za-z]+\s*:\s*[A-Za-z]+', question) and len(question) < 80:
        memory = 5
        logic = 2
        difficulty = "easy"
        expected_time = 25

    score_map = {"very_easy": 0.1, "easy": 0.3, "medium": 0.5, "hard": 0.8, "very_hard": 0.95}
    return difficulty, score_map.get(difficulty, 0.5), memory, logic, calc, expected_time


def classify_subject_chapter(question: str, options: List[Dict]) -> Dict:
    combined = (question + " " + " ".join([o.get("option_text","") for o in options])).lower()

    # Check chapter map first - most specific
    for key, (subj, chap, topic) in CHAPTER_MAP.items():
        if key.lower() in combined:
            return {
                "subject": subj,
                "chapter": chap,
                "topic": topic,
                "subtopic": None,
                "micro_topic": None,
                "concepts": [topic],
                "pattern": key.title(),
                "question_family": chap,
            }

    # Check for number analogy pattern: 132 : 156 :: 462 : ?
    if re.search(r'\d+\s*:\s*\d+\s*::', combined):
        return {
            "subject": "Reasoning",
            "chapter": "Analogy",
            "topic": "Number Analogy",
            "subtopic": "Number Relation",
            "micro_topic": None,
            "concepts": ["Number Analogy", "Arithmetic Relation"],
            "pattern": "Number Relation A:B::C:?",
            "question_family": "Analogy",
        }

    # Pressure : Pascal -> SI Units
    if re.search(r'\bpressure\b.*\bpascal\b|\bforce\b.*\bnewton\b|\benergy\b.*\bjoule\b', combined):
        return {
            "subject": "General Science",
            "chapter": "Units and Measurements",
            "topic": "SI Units",
            "subtopic": "Units",
            "micro_topic": None,
            "concepts": ["SI Units", "Word Association"],
            "pattern": "Word Association",
            "question_family": "Units",
        }

    # Subject detection via keywords - count hits
    best_subject = None
    max_hits = 0
    for subject, keywords in SUBJECT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in combined)
        if hits > max_hits:
            max_hits = hits
            best_subject = subject

    if not best_subject:
        # Fallback heuristics
        if re.search(r'\b\d+\b.*\b\d+\b', combined) and any(w in combined for w in ["find", "what is", "value", "solve"]):
            best_subject = "Quantitative Aptitude"
        elif any(w in combined for w in ["who", "when", "capital", "river", "president", "article"]):
            best_subject = "General Awareness"
        elif any(w in combined for w in ["synonym", "antonym", "sentence", "grammar", "fill"]):
            best_subject = "English"
        elif re.search(r':\s*::', combined) or "related to" in combined or "odd one out" in combined:
            best_subject = "Reasoning"
        else:
            best_subject = "Reasoning"

    return {
        "subject": best_subject,
        "chapter": None,
        "topic": None,
        "subtopic": None,
        "micro_topic": None,
        "concepts": [],
        "pattern": None,
        "question_family": None,
    }


def generate_tags(classification: Dict, metadata: Dict, bloom: str, q_type: str) -> Tuple[List[str], List[str]]:
    tags = []
    keywords = []

    subj = classification.get("subject")
    chap = classification.get("chapter")
    topic = classification.get("topic")
    if subj:
        tags.append(subj)
        keywords.append(subj.lower())
    if chap:
        tags.append(chap)
        keywords.append(chap.lower())
    if topic:
        tags.append(topic)
        keywords.append(topic.lower())

    # Pattern & family
    if classification.get("pattern"):
        tags.append(classification["pattern"])
    if classification.get("question_family"):
        tags.append(classification["question_family"])

    # Concepts as keywords
    if classification.get("concepts"):
        for c in classification["concepts"]:
            keywords.append(c.lower())
            if c not in tags:
                tags.append(c)

    exam = metadata.get("exam_name") or metadata.get("source_book") or ""
    if exam:
        if "SSC" in str(exam).upper():
            tags.extend(["SSC", "PYQ" if metadata.get("exam_year") else "Practice"])
        if "UPSC" in str(exam).upper():
            tags.append("UPSC")
        if any(x in str(exam).upper() for x in ["RRB", "RAILWAY"]):
            tags.append("Railway")
        if "IBPS" in str(exam).upper() or "SBI" in str(exam).upper() or "BANK" in str(exam).upper():
            tags.append("Banking")

    tags.append(bloom.capitalize())
    tags.append(q_type.replace("_", " ").title())

    # Deduplicate preserving order
    seen = set()
    uniq_tags = []
    for t in tags:
        tl = t.lower()
        if tl not in seen and t.strip():
            seen.add(tl)
            uniq_tags.append(t)

    seen_k = set()
    uniq_kw = []
    for k in keywords:
        kl = k.lower()
        if kl not in seen_k and len(kl) > 1:
            seen_k.add(kl)
            uniq_kw.append(kl)

    return uniq_tags[:15], uniq_kw[:20]


def extract_metadata_from_text(text: str) -> Dict:
    meta: Dict[str, any] = {}
    for pat, org in EXAM_PATTERNS:
        try:
            m = re.search(pat, text, re.IGNORECASE)
        except re.error:
            continue
        if m:
            meta["exam_name"] = m.group(1).strip()[:255]
            meta["organization"] = org
            # Try year
            try:
                y = re.search(r"(20\d{2})", m.group(1))
                if y:
                    meta["exam_year"] = int(y.group(1))
            except Exception:
                pass
            break

    # Shift
    try:
        shift_match = re.search(r"(Morning|Evening|Afternoon|Shift\s*\d+|Shift\s*-\s*\d+)", text, re.IGNORECASE)
        if shift_match:
            meta["shift"] = shift_match.group(1).strip()[:64]
    except re.error:
        pass

    # Source book page
    try:
        page_match = re.search(r"Page\s*No\.?\s*(\d+)|Page\s*-\s*(\d+)", text, re.IGNORECASE)
        if page_match:
            num = page_match.group(1) or page_match.group(2)
            if num:
                try:
                    meta["page_number"] = int(num)
                except ValueError:
                    pass
    except re.error:
        pass

    return meta
