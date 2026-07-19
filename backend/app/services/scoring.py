"""Scoring engine for all CBT question types.

Pure functions — no database, no Flask request context. Safe to call from
attempt evaluation, bulk regrade jobs, and unit tests.

Public contract (stable — used by ``app.routes.attempts``)::

    evaluate_answer(question_type, selected, correct, marks, negative_marks)
        -> tuple[Optional[bool], float]

    is_correct:
        True   — awarded full marks
        False  — wrong (negative marks applied when configured)
        None   — unattempted (zero marks, not wrong)

Supported question_type values (case-insensitive)::

    single_choice | multiple_choice | integer | paragraph | image | math

Security / robustness:
    * Never raises on malformed candidate or key data (corrupt bank rows).
    * Bounds string/list sizes to avoid CPU blow-ups on adversarial payloads.
    * Marks coerced to finite floats; NaN/Inf rejected → 0.
    * Integer compare resists leading zeros and whitespace without eval().
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Final, Iterable, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("exam_os.services.scoring")

# Adversarial / corrupt payload bounds
_MAX_ANSWER_CHARS: Final[int] = 2_048
_MAX_MULTI_SELECTIONS: Final[int] = 32
_MAX_CORRECT_KEYS: Final[int] = 32

# Types treated as option-key MCQ (single correct key)
_SINGLE_KEY_TYPES: Final[frozenset[str]] = frozenset({
    "single_choice",
    "paragraph",
    "image",
    "math",
})


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce marks to a finite float; never propagate NaN/Inf into scores."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _clip_str(value: Any, max_len: int = _MAX_ANSWER_CHARS) -> str:
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        return text[:max_len]
    return text


def _normalize(value: Any) -> Union[None, str, List[str]]:
    """
    Normalize an answer token or list of tokens for comparison.

    * None stays None
    * list → sorted unique-preserving multiset of uppercased stripped strings
    * scalar → stripped string (case preserved until final compare for keys)
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        items: List[str] = []
        for index, item in enumerate(value):
            if index >= _MAX_MULTI_SELECTIONS:
                break
            if item is None:
                continue
            token = _clip_str(item).strip().upper()
            if token:
                items.append(token)
        return sorted(items)
    return _clip_str(value).strip()


def _is_unattempted(selected: Any) -> bool:
    if selected is None:
        return True
    if selected == "":
        return True
    if selected == []:
        return True
    if isinstance(selected, (list, tuple, set)) and len(selected) == 0:
        return True
    if isinstance(selected, str) and not selected.strip():
        return True
    return False


def _parse_correct_multi(correct: Any) -> List[str]:
    """Parse multi-correct key into a normalized list of option keys."""
    if correct is None:
        return []
    if isinstance(correct, (list, tuple, set)):
        return _normalize(list(correct)) or []  # type: ignore[return-value]
    if isinstance(correct, str):
        text = _clip_str(correct).strip()
        if not text:
            return []
        # JSON list first
        if text[0] in "[":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return _normalize(parsed) or []  # type: ignore[return-value]
            except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
                pass
        # Comma-separated CSV style
        if "," in text:
            parts = [p.strip() for p in text.split(",") if p.strip()]
            return _normalize(parts) or []  # type: ignore[return-value]
        return _normalize([text]) or []  # type: ignore[return-value]
    return _normalize([correct]) or []  # type: ignore[return-value]


def _negative_award(negative_marks: float) -> float:
    neg = _finite_float(negative_marks, 0.0)
    if neg <= 0:
        return 0.0
    return -abs(neg)


def _positive_award(marks: float) -> float:
    m = _finite_float(marks, 0.0)
    if m < 0:
        # Defensive: never award "negative full marks" as a correct score
        return 0.0
    return m


def _integers_equal(selected: str, correct: str) -> bool:
    """
    Compare integer answers without ``eval``.

    Accepts optional leading +/-, whitespace already stripped by caller.
    Leading zeros: ``08`` == ``8``. Rejects non-integer decimals.
    """
    if selected == correct:
        return True

    def parse_int(token: str) -> Optional[int]:
        if not token:
            return None
        # Bound length — huge digit strings are a CPU DOS vector for int()
        if len(token) > 64:
            return None
        sign = 1
        body = token
        if body[0] in "+-":
            if body[0] == "-":
                sign = -1
            body = body[1:]
        if not body or not body.isdigit():
            return None
        try:
            return sign * int(body, 10)
        except ValueError:
            return None

    left = parse_int(selected)
    right = parse_int(correct)
    if left is None or right is None:
        return False
    return left == right


def evaluate_answer(
    question_type: str,
    selected: Any,
    correct: Any,
    marks: float,
    negative_marks: float,
    partial_marking: bool = False,
) -> Tuple[Optional[bool], float]:
    """
    Evaluate one candidate response against the answer key.

    Parameters
    ----------
    question_type:
        CBT type string (see module docstring).
    selected:
        Candidate response (option key, list of keys, integer string, …).
    correct:
        Authoritative key from the question bank.
    marks:
        Marks for a fully correct response (must be >= 0 to award).
    negative_marks:
        Penalty magnitude for a wrong attempt (applied as negative score).

    Returns
    -------
    (is_correct, marks_awarded)
        is_correct is None when unattempted.
    """
    try:
        if _is_unattempted(selected):
            return None, 0.0

        qtype = (question_type or "single_choice").strip().lower()
        pos = _positive_award(marks)
        neg = _negative_award(negative_marks)

        # ----- Integer -----
        if qtype == "integer":
            sel = _clip_str(selected).strip()
            cor = _clip_str(correct).strip() if correct is not None else ""
            if not cor:
                # Missing key — do not punish student for bank error
                logger.warning("integer question missing correct key; awarding 0")
                return False, 0.0
            if _integers_equal(sel, cor):
                return True, pos
            return False, neg

        # ----- Multiple correct options -----
        if qtype == "multiple_choice":
            if isinstance(selected, list):
                sel_list = selected
            elif isinstance(selected, (tuple, set)):
                sel_list = list(selected)
            else:
                # Single token submitted for multi — treat as one selection
                sel_list = [selected]
            sel = _normalize(sel_list)
            cor = _parse_correct_multi(correct)
            if not cor:
                logger.warning("multiple_choice missing correct keys; awarding 0")
                return False, 0.0
            if sel == cor:
                return True, pos
            # Optional partial credit when configured (no wrong extras)
            if partial_marking and cor:
                sel_set = set(sel or [])
                cor_set = set(cor)
                extras = sel_set - cor_set
                if not extras and sel_set:
                    matched = len(sel_set & cor_set)
                    if matched == len(cor_set):
                        return True, pos
                    if matched > 0:
                        return True, round(pos * (matched / len(cor_set)), 4)
            return False, neg

        # ----- Single key: single_choice, paragraph, image, math (+ unknown) -----
        # Unknown types fall through to key compare (safe default for future types)
        sel = _normalize(selected)
        cor = _normalize(correct)

        if isinstance(sel, list):
            sel = sel[0] if sel else None
        if isinstance(cor, list):
            cor = cor[0] if cor else None

        if sel is None or cor is None or cor == "":
            if cor is None or cor == "":
                logger.warning(
                    "single-key question type=%s missing correct key; awarding 0",
                    qtype,
                )
                return False, 0.0
            return False, neg

        sel_key = str(sel).strip().upper()
        cor_key = str(cor).strip().upper()
        if sel_key == cor_key:
            return True, pos
        return False, neg

    except Exception:  # noqa: BLE001 — scoring must never crash attempt submit
        logger.exception(
            "evaluate_answer unexpected failure type=%s",
            question_type,
        )
        # Fail closed for marks (0) but mark as wrong so analytics stay consistent
        return False, 0.0


# --------------------------------------------
# EXTENSION POINT: register custom type scorers
# map[str, Callable] without changing evaluate_answer signature
# --------------------------------------------
