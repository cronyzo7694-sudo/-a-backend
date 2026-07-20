"""
Layer 3: EXTRACTOR - Understand content like human teacher, not parser.
Never assume fixed structure. Handles broken OCR, missing spaces, etc.
Never guess answer if missing.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple, Optional


def extract_options(block: str) -> Tuple[List[Dict], str]:
    """
    Extract options A-D, 1-4, (a)-(d), etc
    Returns (options, remaining_text)
    """
    patterns = [
        # A) text newline B) text ...
        r"\n\s*\(?([A-D])\)?[\.\)\:\s]+\s*([^\n]+?)(?=\n\s*\(?[A-D]\)?[\.\)\:\s]+|\n\s*(?:Answer|Ans|Explanation|Solution|Correct)|\Z)",
        # 1) text newline 2) ...
        r"\n\s*([1-4])[\.\)\s]+\s*([^\n]+?)(?=\n\s*[1-4][\.\)\s]+|\n\s*(?:Answer|Ans|Explanation|Solution)|\Z)",
        # (a) text newline (b) ...
        r"\n\s*\(?\s*([a-d])\s*\)?[\.\)\s]+\s*([^\n]+?)(?=\n\s*\(?\s*[a-d]\s*\)?[\.\)\s]+|\n\s*(?:Answer|Ans|Explanation)|\Z)",
        # Inline: A) School B) Classroom C) Student D) Book
        # We'll handle separately
    ]

    options = []
    clean_block = block

    # Try multiline patterns first
    for pattern in patterns:
        try:
            matches = re.findall(pattern, block, re.DOTALL | re.IGNORECASE)
        except re.error:
            continue
        if len(matches) >= 2:
            for key, text_content in matches[:6]:
                text_content = text_content.strip()
                if re.match(r"(?i)^(Answer|Ans|Explanation|Solution|Correct|Exp\.?|Sol\.?)", text_content):
                    continue
                # Clean option text - take first line, limit length
                first_line = text_content.split("\n")[0].strip()
                # Remove trailing junk
                first_line = re.split(r"\s*(?:Answer|Explanation|Solution)", first_line, flags=re.IGNORECASE)[0].strip()
                if len(first_line) > 5:  # plausible option
                    options.append({
                        "option_key": key.upper() if isinstance(key, str) and key.isalpha() else str(key),
                        "option_text": first_line[:500],
                        "option_html": None,
                        "image_url": None,
                        "is_correct": False,
                        "raw": f"{key}) {first_line}"
                    })
            if options:
                # Find first option occurrence to split question
                try:
                    first_opt_match = re.search(pattern, block, re.DOTALL | re.IGNORECASE)
                    if first_opt_match:
                        clean_block = block[:first_opt_match.start()].strip()
                except re.error:
                    clean_block = block.split(options[0]["raw"])[0].strip() if options else block
                break

    # If no options found with multiline, try inline pattern: A) School B) Classroom C) Student D) Book
    if len(options) < 2:
        # Look for inline options in whole block
        inline_pattern = r"(?:^|\s)([A-D])[\.\)\]]\s+([^A-D\n]+?)(?=\s+[A-D][\.\)\]]\s+|\s*(?:Answer|Ans|Explanation|Solution)|\s*$)"
        # Better: find all occurrences of A) ... B) ... etc in same line
        try:
            # Extract line that contains options
            lines = block.split("\n")
            for idx, line in enumerate(lines):
                # Count A) B) C) D) in line
                inline_matches = re.findall(r"([A-D])[\.\)\]]\s+([^A-D]+?)(?=\s+[A-D][\.\)\]]|\s*$)", line, flags=re.IGNORECASE)
                if len(inline_matches) >= 2:
                    for key, txt in inline_matches[:4]:
                        txt_clean = txt.strip().split("Answer")[0].strip().split("Explanation")[0].strip()[:200]
                        if txt_clean:
                            options.append({
                                "option_key": key.upper(),
                                "option_text": txt_clean,
                                "option_html": None,
                                "image_url": None,
                                "is_correct": False,
                                "raw": f"{key}) {txt_clean}"
                            })
                    clean_block = "\n".join(lines[:idx]).strip()
                    break
        except re.error:
            pass

    # Normalize option keys: 1->A, 2->B etc
    num_to_letter = {"1": "A", "2": "B", "3": "C", "4": "D"}
    normalized_options = []
    for opt in options:
        key = opt["option_key"]
        key = num_to_letter.get(key, key).upper()
        if key not in [o["option_key"] for o in normalized_options]:  # deduplicate
            opt["option_key"] = key
            normalized_options.append(opt)

    return normalized_options, clean_block


def extract_correct_answer(block: str, options: List[Dict]) -> Optional[str]:
    patterns = [
        r"(?:Answer|Ans|Correct\s*Answer|Solution|Answer\s*Key)\s*[:\-\s]*\(?([A-D\d])\)?",
        r"Answer\s*Key\s*[:\-\s]*\(?([A-D\d])\)?",
        r"Correct\s*Option\s*[:\-\s]*\(?([A-D\d])\)?",
        r"Option\s*\(?([A-D])\)?\s*is\s*correct",
        r"Answer\s+is\s+\(?([A-D\d])\)?",
        r"Right\s+Answer\s*[:\-]\s*\(?([A-D\d])\)?",
        r"\(?([A-D])\)?\s*is\s*the\s*correct",
    ]
    for pat in patterns:
        try:
            m = re.search(pat, block, re.IGNORECASE)
        except re.error:
            continue
        if m:
            ans = m.group(1).strip().upper()
            num_to_letter = {"1": "A", "2": "B", "3": "C", "4": "D"}
            ans = num_to_letter.get(ans, ans)
            for opt in options:
                if opt["option_key"] == ans or opt["option_key"] == str(ans):
                    opt["is_correct"] = True
            return ans
    return None


def extract_explanation(block: str) -> Tuple[Optional[str], str]:
    patterns = [
        r"(?:Explanation|Describe|Reason|Solution|Concept|Exp\.?|Sol\.?)\s*[:\-\s]*\n?(.*)",
    ]
    for pat in patterns:
        try:
            m = re.search(pat, block, re.DOTALL | re.IGNORECASE)
        except re.error:
            continue
        if m:
            exp = m.group(1).strip()
            # Cut at reasonable length and remove following question if any
            exp = exp.split("\n\n")[0]
            exp = re.split(r"\n\s*(?:Q\.?\s*No\.?\s*|Q\.\s*\d+)", exp, flags=re.IGNORECASE)[0]
            if len(exp) > 20:
                remaining = block[:m.start()].strip()
                return exp[:2000].strip(), remaining
    return None, block


def extract_passage(block: str) -> Tuple[Optional[Dict], str]:
    passage_keywords = ["passage", "read the following", "study the following", "comprehension", "read the passage"]
    lower = block.lower()
    if any(k in lower for k in passage_keywords):
        parts = block.split("\n\n")
        if len(parts) >= 2 and len(parts[0]) > 150:
            passage_text = parts[0]
            question_text = "\n\n".join(parts[1:])
            if "?" not in passage_text or passage_text.count(".") >= 2:
                return {"text": passage_text[:2000], "html": None, "images": []}, question_text
    return None, block


def extract_assertion_reason(block: str) -> Dict:
    result = {}
    try:
        a_match = re.search(r"Assertion\s*\(A\)\s*[:\-\s]*(.+?)(?=\n\s*Reason|R\s*:\s*|\n\s*[A-D]\))", block, re.DOTALL | re.IGNORECASE)
        r_match = re.search(r"Reason\s*\(R\)\s*[:\-\s]*(.+?)(?=\n\s*[A-D]\))", block, re.DOTALL | re.IGNORECASE)
        if a_match and r_match:
            result["assertion"] = a_match.group(1).strip()[:1000]
            result["reason"] = r_match.group(1).strip()[:1000]
            result["question_type"] = "assertion_reason"
    except re.error:
        pass
    return result


def extract_statements(block: str) -> Optional[List[str]]:
    try:
        stmt_pattern = re.findall(r"Statement\s*\d+\s*[:\-\s]*(.+)", block, re.IGNORECASE)
        if len(stmt_pattern) >= 2:
            return [s.strip()[:1000] for s in stmt_pattern[:6]]
    except re.error:
        pass
    return None


def extract_matching(block: str) -> Optional[Dict]:
    if re.search(r"Match\s+the\s+following|Column\s+A.*Column\s+B|Match\s+Column", block, re.IGNORECASE):
        # Very basic extraction - full parsing would need table detection
        return {"detected": True, "raw": block[:1000]}
    return None


def detect_question_type(block: str, has_options: bool, assertion_data: Dict, statements: Optional[List[str]], matching: Optional[Dict]) -> str:
    if assertion_data:
        return "assertion_reason"
    if matching:
        return "match_the_following"
    if statements and len(statements) >= 2:
        return "statement_based"
    if re.search(r"fill\s+in\s+the\s+blank|___|____|\(\s*\)\s*blank", block, re.IGNORECASE):
        return "fill_blank"
    if re.search(r"^\s*\d+\s*$", block.strip(), re.MULTILINE):
        return "integer"
    if has_options:
        if re.search(r"select\s+all|both|all\s+of\s+the\s+above|multiple.*correct", block, re.IGNORECASE):
            return "multiple_choice"
        return "single_choice"
    # If contains table or code pattern
    if re.search(r"def\s+\w+|class\s+\w+|for\s*\(.*\)|```", block):
        return "single_choice"  # but could be code, for now treat as single
    return "single_choice"


def human_like_extract(raw_block: str) -> Dict:
    raw = raw_block

    # Passage
    passage, without_passage = extract_passage(raw_block)

    # Assertion/Reason
    assertion_data = extract_assertion_reason(without_passage)

    # Statements
    statements = extract_statements(without_passage)

    # Matching
    matching = extract_matching(without_passage)

    # Options
    options, question_without_options = extract_options(without_passage)

    # Correct answer
    correct = extract_correct_answer(raw_block, options)

    # Explanation
    explanation, question_core = extract_explanation(question_without_options)

    if not question_core or len(question_core.strip()) < 10:
        question_core = question_without_options

    # Type
    q_type = detect_question_type(raw_block, len(options) > 0, assertion_data, statements, matching)

    return {
        "raw_text": raw,
        "question_text": question_core.strip()[:5000] if question_core else None,
        "options": options,
        "correct_answer": correct,  # None if not found - NEVER guess
        "explanation": explanation,
        "paragraph": passage,
        "assertion": assertion_data.get("assertion"),
        "reason": assertion_data.get("reason"),
        "statements": statements,
        "matching_raw": matching,
        "question_type": q_type,
    }
