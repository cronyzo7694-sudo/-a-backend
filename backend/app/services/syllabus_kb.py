"""
Canonical syllabus knowledge base (subject -> chapters).

Used to decide, for a given exam, WHICH subjects/chapters/topics are expected,
so we can:
  * build topic/chapter tests for whatever questions exist in the file bank,
  * build a SUBJECT test only when ALL chapters of that subject are covered,
  * build a FULL test only when ALL subjects of the exam are covered.

Matching is done on normalized (lowercased, alias-mapped) names so the file
bank's chapter/topic names line up with the exam syllabus.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Canonical subject -> list of canonical chapters (SSC-style; extend freely).
SUBJECT_CHAPTERS: Dict[str, List[str]] = {
    "Reasoning": [
        "Analogy", "Classification", "Series", "Coding-Decoding", "Blood Relations",
        "Direction Sense", "Ranking", "Calendar", "Clock", "Venn Diagram",
        "Syllogism", "Statement and Conclusion", "Figure Based", "Dice", "Cube",
        "Mirror Image", "Paper Folding", "Mathematical Operations",
    ],
    "Quantitative Aptitude": [
        "Number System", "Simplification", "Percentage", "Profit and Loss",
        "Simple Interest", "Compound Interest", "Ratio and Proportion", "Average",
        "Time and Work", "Pipes and Cistern", "Time Speed Distance", "Boat and Stream",
        "Partnership", "Algebra", "Geometry", "Mensuration", "Trigonometry",
        "Coordinate Geometry", "Statistics", "Data Interpretation",
    ],
    "English": [
        "Grammar", "Vocabulary", "Synonyms", "Antonyms", "Idioms and Phrases",
        "Error Detection", "Fill in the Blanks", "Cloze Test", "One Word Substitution",
        "Active Passive", "Narration", "Para Jumble", "Reading Comprehension",
        "Spellings",
    ],
    "General Awareness": [
        "History", "Geography", "Polity", "Economics", "Science", "Current Affairs",
        "Static GK", "Books and Authors", "Awards", "Sports", "Important Days",
        "Art and Culture",
    ],
    "General Science": [
        "Physics", "Chemistry", "Biology", "Units and Measurements", "Space",
    ],
    "Computer": [
        "Computer Basics", "Hardware", "Software", "MS Word", "MS Excel",
        "MS PowerPoint", "Internet", "Networking", "Memory", "Operating System",
        "Virus", "Email",
    ],
}

# Alias -> canonical (both subjects and chapters). Lowercased keys.
_ALIASES = {
    # subjects
    "reasoning": "Reasoning", "resoning": "Reasoning", "logical reasoning": "Reasoning",
    "quant": "Quantitative Aptitude", "quantitative aptitude": "Quantitative Aptitude",
    "maths": "Quantitative Aptitude", "math": "Quantitative Aptitude",
    "mathematics": "Quantitative Aptitude", "arithmetic": "Quantitative Aptitude",
    "english": "English",
    "general awareness": "General Awareness", "gk": "General Awareness",
    "general knowledge": "General Awareness", "ga": "General Awareness",
    "general studies": "General Awareness", "gs": "General Awareness",
    "general science": "General Science", "science": "General Science",
    "computer": "Computer", "computer knowledge": "Computer",
    # common chapter aliases
    "profit loss": "Profit and Loss", "profit & loss": "Profit and Loss",
    "profit and loss": "Profit and Loss",
    "si": "Simple Interest", "ci": "Compound Interest",
    "si ci": "Simple Interest", "si/ci": "Simple Interest",
    "time work": "Time and Work", "time & work": "Time and Work",
    "time speed distance": "Time Speed Distance", "speed distance": "Time Speed Distance",
    "tsd": "Time Speed Distance",
    "ratio": "Ratio and Proportion",
    "coding decoding": "Coding-Decoding", "coding": "Coding-Decoding",
    "blood relation": "Blood Relations", "blood relations": "Blood Relations",
    "direction": "Direction Sense",
    "di": "Data Interpretation",
    "units": "Units and Measurements", "si units": "Units and Measurements",
    "idiom": "Idioms and Phrases", "phrase": "Idioms and Phrases",
}


def normalize(name: Optional[str]) -> str:
    if not name:
        return ""
    n = re.sub(r"\s+", " ", str(name)).strip().lower()
    n = n.replace("&", " and ")
    n = re.sub(r"\s+", " ", n).strip()
    return n


def canon_subject(name: Optional[str]) -> Optional[str]:
    n = normalize(name)
    if not n:
        return None
    if n in _ALIASES and _ALIASES[n] in SUBJECT_CHAPTERS:
        return _ALIASES[n]
    for subj in SUBJECT_CHAPTERS:
        if normalize(subj) == n:
            return subj
    # partial contains
    for subj in SUBJECT_CHAPTERS:
        if normalize(subj) in n or n in normalize(subj):
            return subj
    return None


def canon_chapter(name: Optional[str]) -> Optional[str]:
    n = normalize(name)
    if not n:
        return None
    if n in _ALIASES:
        return _ALIASES[n]
    for subj, chaps in SUBJECT_CHAPTERS.items():
        for ch in chaps:
            if normalize(ch) == n:
                return ch
    for subj, chaps in SUBJECT_CHAPTERS.items():
        for ch in chaps:
            cn = normalize(ch)
            if cn in n or n in cn:
                return ch
    return None


def chapters_for_subject(subject: str) -> List[str]:
    return SUBJECT_CHAPTERS.get(subject, [])


def subject_of_chapter(chapter: str) -> Optional[str]:
    cc = canon_chapter(chapter)
    if not cc:
        return None
    for subj, chaps in SUBJECT_CHAPTERS.items():
        if cc in chaps:
            return subj
    return None
