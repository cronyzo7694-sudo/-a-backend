"""
Layer 2: PREPROCESSOR - Remove junk, fix OCR, normalize, preserve raw_text
Does NOT assume any fixed structure.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Dict, List, Tuple

from .constants import JUNK_PATTERNS, HEADER_FOOTER_PATTERNS, OCR_FIXES


def clean_junk(text: str) -> Tuple[str, Dict]:
    original_len = len(text)
    removed = []

    # Unicode normalize
    text = unicodedata.normalize("NFKC", text)
    text = text.replace('\u00A0', ' ')  # NBSP
    text = text.replace('ﬁ', 'fi').replace('ﬂ', 'fl')

    # Fix common OCR mistakes
    for pattern, repl in OCR_FIXES.items():
        try:
            new_text, n = re.subn(pattern, repl, text)
            if n > 0:
                text = new_text
        except re.error:
            continue

    # Remove junk
    for pattern in JUNK_PATTERNS:
        try:
            matches = re.findall(pattern, text, flags=re.IGNORECASE)
            if matches:
                removed.extend(matches[:5])
                text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        except re.error:
            continue

    # Remove repeated newlines but keep para breaks
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Clean header/footer per line
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append(line)
            continue
        is_junk_line = False
        for pat in HEADER_FOOTER_PATTERNS:
            try:
                if re.match(pat, stripped, re.IGNORECASE):
                    removed.append(stripped)
                    is_junk_line = True
                    break
            except re.error:
                continue
        if not is_junk_line:
            cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()

    stats = {
        "original_len": original_len,
        "cleaned_len": len(cleaned),
        "junk_removed_sample": removed[:10],
        "junk_count": len(removed),
    }
    return cleaned, stats


def detect_language(text: str) -> str:
    devanagari_count = len(re.findall(r'[\u0900-\u097F]', text))
    total_chars = len(text) if text else 1
    if devanagari_count == 0:
        return "en"
    ratio = devanagari_count / total_chars
    if ratio > 0.3:
        eng_words = len(re.findall(r'\b[A-Za-z]{3,}\b', text))
        if eng_words > 10:
            return "hi-en"
        return "hi"
    return "en"


def extract_question_blocks(text: str) -> List[str]:
    """
    Never assume fixed structure. Try multiple strategies.
    """
    text = text.strip()
    if not text:
        return []

    # Strategy 1: Numbered Q. 1., Q1, 1), (1), Q.No 1
    pattern1 = re.compile(
        r"(?:^|\n)\s*(?:Q\.?\s*No\.?\s*|Q\.\s*|Question\s*)?(\d+)[\.\)\]]\s+(.*?)(?=(?:\n\s*(?:Q\.?\s*No\.?\s*|Q\.\s*|Question\s*)?\d+[\.\)\]]\s+)|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    try:
        blocks = pattern1.findall(text)
        if blocks and len(blocks) >= 2:
            # blocks is list of tuples (number, content)
            return [b[1].strip() if isinstance(b, tuple) and len(b) > 1 else str(b).strip() for b in blocks if (b[1].strip() if isinstance(b, tuple) else str(b).strip())]
    except re.error:
        pass

    # Strategy 2: Split by problem separators - e.g., numbered with line breaks and question marks
    # Try to find chunks that look like questions (contain ? or options)
    chunks = re.split(r"\n\s*\n", text)
    questions = []
    current_block = ""
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        # If chunk looks like start of new question (starts with number or contains question keyword)
        if re.match(r"^\s*(?:\d+[\.\)\]]|Q\.?\s*\d+)", chunk):
            if current_block and len(current_block) > 30:
                questions.append(current_block)
            current_block = chunk
        else:
            # Check if it's option or continuation
            if re.match(r"^\s*(?:[A-D][\.\)\]]|\(\s*[A-D]\s*\)|\d+[\.\)\]])", chunk):
                current_block += "\n" + chunk
            else:
                if "?" in chunk or len(chunk) < 300:
                    current_block += "\n\n" + chunk
                else:
                    # Long paragraph maybe passage - keep together
                    if current_block:
                        current_block += "\n\n" + chunk
                    else:
                        current_block = chunk

        # If block contains question mark and at least some options or length, consider boundary
        if len(current_block) > 1000:
            questions.append(current_block)
            current_block = ""

    if current_block and len(current_block.strip()) > 20:
        questions.append(current_block)

    # Filter to keep only plausible question blocks
    filtered = [q for q in questions if len(q) > 20 and (("?" in q) or ("A)" in q) or ("A." in q) or len(q.split()) > 10)]

    if len(filtered) >= 1:
        return filtered

    # Strategy 3: Fallback - whole text as single question if looks like question
    if len(text) > 20:
        return [text]

    return []


def normalize_spacing(text: str) -> str:
    text = re.sub(r'([.?!,;:])([A-Z\u0900-\u097F])', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def generate_hashes(text: str) -> Dict[str, str]:
    normalized = re.sub(r'\W+', '', text.lower())
    fingerprint = hashlib.sha256(normalized.encode('utf-8', errors='ignore')).hexdigest()

    words = re.findall(r'\b\w+\b', text.lower())
    stop = {"the", "is", "are", "of", "a", "an", "in", "to", "for", "what", "which", "who", "is", "are"}
    filtered_words = [w for w in words if w not in stop and len(w) > 1]
    semantic_core = " ".join(sorted(set(filtered_words)))
    semantic_hash = hashlib.sha256(semantic_core.encode('utf-8', errors='ignore')).hexdigest()

    source_hash = hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()

    return {
        "fingerprint_hash": fingerprint,
        "semantic_hash": semantic_hash,
        "source_hash": source_hash,
        "normalized_core": normalized[:100],
    }
