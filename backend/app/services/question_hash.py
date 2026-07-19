"""Question fingerprinting for duplicate detection and bank integrity."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable, List, Optional, Sequence


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_question_text(text: Optional[str]) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).lower().strip()
    value = _PUNCT_RE.sub(" ", value)
    value = _WS_RE.sub(" ", value).strip()
    return value


def normalize_options(options: Optional[Sequence[Any]]) -> List[str]:
    out: List[str] = []
    if not options:
        return out
    for opt in options:
        if isinstance(opt, dict):
            t = opt.get("option_text") or opt.get("text") or ""
        else:
            t = opt
        n = normalize_question_text(str(t) if t is not None else "")
        if n:
            out.append(n)
    return sorted(out)


def compute_question_hash(
    question_text: str,
    options: Optional[Sequence[Any]] = None,
    question_type: str = "single_choice",
    correct_answer: Any = None,
) -> str:
    """
    Stable SHA-256 fingerprint for duplicate detection.

    Includes normalized stem + options + type (not marks/explanation) so the
    same academic item maps once across banks/exams.
    """
    payload = {
        "t": normalize_question_text(question_text),
        "o": normalize_options(options),
        "q": (question_type or "single_choice").strip().lower(),
        # correct answer intentionally excluded from hash so re-keying
        # does not fork duplicates; detection is content-based
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_question_hash_from_model(question: Any) -> str:
    opts = []
    try:
        for o in getattr(question, "options", None) or []:
            opts.append({
                "option_key": getattr(o, "option_key", None),
                "option_text": getattr(o, "option_text", None),
            })
    except Exception:
        opts = []
    return compute_question_hash(
        getattr(question, "question_text", "") or "",
        opts,
        getattr(question, "question_type", "single_choice") or "single_choice",
        getattr(question, "correct_answer", None),
    )
