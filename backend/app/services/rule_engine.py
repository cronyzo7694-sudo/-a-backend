"""Configuration-driven Exam Rule Engine.

All exam behaviour is derived from ``Exam.rules_json`` merged with
``DEFAULT_EXAM_RULES``. Routes must not hardcode SSC/UPSC-specific branches;
call ``ExamRuleEngine.from_exam(exam)`` instead.

Public API (stable helpers)::

    DEFAULT_EXAM_RULES
    merge_exam_rules(raw) -> dict
    ExamRuleEngine.from_exam(exam)
    engine.get(path, default=None)
    engine.to_public_dict()
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Mapping, MutableMapping, Optional

logger = logging.getLogger("exam_os.services.rule_engine")

# ---------------------------------------------------------------------------
# Canonical default rule pack — every exam inherits these unless overridden
# ---------------------------------------------------------------------------
DEFAULT_EXAM_RULES: Dict[str, Any] = {
    "version": 1,
    "timing": {
        "overall_seconds": None,  # None → use Exam.duration_seconds
        "auto_submit_on_expiry": True,
        "allow_pause": False,
        "max_pauses": 0,
        "section_timers": False,  # True → each section uses its duration_seconds
        "section_auto_submit_on_expiry": True,
        "warnings": [
            {"at_percent": 25, "level": "info"},
            {"at_percent": 10, "level": "warning"},
            {"at_percent": 5, "level": "critical"},
            {"at_seconds": 60, "level": "final"},
        ],
    },
    "sections": {
        "strict_order": False,  # sequential unlock
        "lock_on_complete": False,  # cannot return after leaving
        "lock_on_timer_expiry": True,
        "allow_previous_section": True,
        "allow_next_section": True,
        "show_section_tabs": True,
    },
    "navigation": {
        "allow_skip": True,
        "allow_review": True,
        "allow_mark_for_review": True,
        "allow_clear_response": True,
        "free_question_navigation": True,
        "resume_allowed": True,
    },
    "questions": {
        "shuffle_questions": False,  # overridden by Exam.shuffle_questions if set
        "shuffle_options": False,
        "mandatory_question_ids": [],
        "optional_question_ids": [],
    },
    "marking": {
        "negative_marking": True,
        "default_marks": None,  # Exam.default_marks
        "default_negative_marks": None,
        "partial_marking": False,
        "unattempted_marks": 0,
    },
    "language": {
        "allowed": ["en"],
        "default": "en",
        "allow_switch": False,
    },
    "security": {
        "require_fullscreen": False,
        "detect_tab_switch": True,
        "max_tab_switches": None,  # Exam.max_tab_switches
        "force_submit_on_max_tab_switches": True,
        "prevent_copy": True,
        "prevent_paste": True,
        "prevent_right_click": True,
        "block_devtools": False,
    },
    "aids": {
        "calculator_allowed": False,
        "rough_sheet_allowed": True,
        "virtual_calculator": False,
    },
    "modes": {
        # practice | mock | sectional | pyq | live — Exam.exam_mode is source of truth
        "show_answer_after_each": False,  # practice convenience
        "show_solution_after_submit": True,
        "allow_multiple_attempts": True,
        "max_attempts_per_user": 0,  # 0 = unlimited
    },
    "submission": {
        "confirm_before_submit": True,
        "warn_unanswered": True,
        "warn_marked_review": True,
        "auto_submit_on_expiry": True,
    },
    "result": {
        "show_immediate": True,  # Exam.show_result_immediately
        "show_correct_answers": True,
        "show_explanations": True,
        "show_section_breakdown": True,
        "show_time_analysis": True,
        "show_percentile": True,
        "show_rank_prediction": True,
    },
    "ui": {
        "show_question_palette": True,
        "palette_position": "right",
        "show_timer": True,
        "theme": "default",
    },
}


def _deep_merge(base: MutableMapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into a deep copy of base."""
    result: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in (override or {}).items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_exam_rules(raw: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Merge operator rules onto DEFAULT_EXAM_RULES."""
    if not raw or not isinstance(raw, Mapping):
        return copy.deepcopy(DEFAULT_EXAM_RULES)
    try:
        return _deep_merge(DEFAULT_EXAM_RULES, dict(raw))
    except Exception:
        logger.exception("merge_exam_rules failed; using defaults")
        return copy.deepcopy(DEFAULT_EXAM_RULES)


def _dig(data: Mapping[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


class ExamRuleEngine:
    """Read-only view over merged exam rules + Exam column fallbacks."""

    def __init__(self, rules: Mapping[str, Any], exam: Any = None):
        self._rules = merge_exam_rules(rules)
        self._exam = exam

    @classmethod
    def from_exam(cls, exam: Any) -> "ExamRuleEngine":
        raw = {}
        try:
            if exam is not None and hasattr(exam, "get_rules"):
                raw = exam.get_rules() or {}
            elif exam is not None and getattr(exam, "rules_json", None):
                import json

                raw = json.loads(exam.rules_json) if exam.rules_json else {}
        except Exception:
            raw = {}
        return cls(raw, exam=exam)

    @classmethod
    def from_dict(cls, rules: Optional[Mapping[str, Any]] = None) -> "ExamRuleEngine":
        return cls(rules or {})

    def get(self, path: str, default: Any = None) -> Any:
        return _dig(self._rules, path, default)

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._rules)

    # ----- Timing -----
    def overall_seconds(self) -> int:
        configured = self.get("timing.overall_seconds")
        if configured is not None:
            try:
                return max(1, int(configured))
            except (TypeError, ValueError):
                pass
        if self._exam is not None:
            try:
                return max(1, int(self._exam.duration_seconds or 3600))
            except (TypeError, ValueError):
                pass
        return 3600

    def auto_submit_on_expiry(self) -> bool:
        v = self.get("timing.auto_submit_on_expiry")
        if v is None:
            v = self.get("submission.auto_submit_on_expiry", True)
        return bool(v)

    def section_timers_enabled(self) -> bool:
        return bool(self.get("timing.section_timers", False))

    def section_auto_submit(self) -> bool:
        return bool(self.get("timing.section_auto_submit_on_expiry", True))

    def allow_pause(self) -> bool:
        return bool(self.get("timing.allow_pause", False))

    def max_pauses(self) -> int:
        try:
            return max(0, int(self.get("timing.max_pauses", 0) or 0))
        except (TypeError, ValueError):
            return 0

    # ----- Sections / navigation -----
    def strict_sections(self) -> bool:
        if self._exam is not None and getattr(self._exam, "strict_sections", None):
            return True
        return bool(self.get("sections.strict_order", False))

    def lock_on_complete(self) -> bool:
        return bool(self.get("sections.lock_on_complete", False)) or self.strict_sections()

    def allow_previous_section(self) -> bool:
        if self.strict_sections() or self.lock_on_complete():
            return False
        return bool(self.get("sections.allow_previous_section", True))

    def allow_next_section(self) -> bool:
        return bool(self.get("sections.allow_next_section", True))

    def resume_allowed(self) -> bool:
        return bool(self.get("navigation.resume_allowed", True))

    def allow_mark_for_review(self) -> bool:
        return bool(self.get("navigation.allow_mark_for_review", True))

    def free_question_navigation(self) -> bool:
        return bool(self.get("navigation.free_question_navigation", True))

    # ----- Shuffle -----
    def shuffle_questions(self) -> bool:
        if self._exam is not None and getattr(self._exam, "shuffle_questions", False):
            return True
        return bool(self.get("questions.shuffle_questions", False))

    def shuffle_options(self) -> bool:
        if self._exam is not None and getattr(self._exam, "shuffle_options", False):
            return True
        return bool(self.get("questions.shuffle_options", False))

    # ----- Marking -----
    def negative_marking_enabled(self) -> bool:
        return bool(self.get("marking.negative_marking", True))

    def partial_marking(self) -> bool:
        return bool(self.get("marking.partial_marking", False))

    def default_marks(self) -> float:
        v = self.get("marking.default_marks")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
        if self._exam is not None:
            try:
                return float(self._exam.default_marks or 1.0)
            except (TypeError, ValueError):
                pass
        return 1.0

    def default_negative_marks(self) -> float:
        if not self.negative_marking_enabled():
            return 0.0
        v = self.get("marking.default_negative_marks")
        if v is not None:
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                pass
        if self._exam is not None:
            try:
                return abs(float(self._exam.default_negative_marks or 0.0))
            except (TypeError, ValueError):
                pass
        return 0.0

    def mandatory_question_ids(self) -> List[int]:
        raw = self.get("questions.mandatory_question_ids") or []
        out: List[int] = []
        if isinstance(raw, list):
            for x in raw:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    continue
        return out

    def optional_question_ids(self) -> List[int]:
        raw = self.get("questions.optional_question_ids") or []
        out: List[int] = []
        if isinstance(raw, list):
            for x in raw:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    continue
        return out

    # ----- Language -----
    def allowed_languages(self) -> List[str]:
        raw = self.get("language.allowed") or ["en"]
        if not isinstance(raw, list) or not raw:
            return ["en"]
        return [str(x) for x in raw]

    def default_language(self) -> str:
        return str(self.get("language.default") or "en")

    def allow_language_switch(self) -> bool:
        return bool(self.get("language.allow_switch", False))

    # ----- Security / aids -----
    def require_fullscreen(self) -> bool:
        if self._exam is not None and getattr(self._exam, "require_fullscreen", False):
            return True
        return bool(self.get("security.require_fullscreen", False))

    def max_tab_switches(self) -> int:
        v = self.get("security.max_tab_switches")
        if v is not None:
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                pass
        if self._exam is not None:
            try:
                return max(0, int(self._exam.max_tab_switches or 5))
            except (TypeError, ValueError):
                pass
        return 5

    def detect_tab_switch(self) -> bool:
        return bool(self.get("security.detect_tab_switch", True))

    def force_submit_on_max_tabs(self) -> bool:
        return bool(self.get("security.force_submit_on_max_tab_switches", True))

    def calculator_allowed(self) -> bool:
        return bool(self.get("aids.calculator_allowed", False))

    def rough_sheet_allowed(self) -> bool:
        return bool(self.get("aids.rough_sheet_allowed", True))

    # ----- Modes -----
    def exam_mode(self) -> str:
        if self._exam is not None and getattr(self._exam, "exam_mode", None):
            return str(self._exam.exam_mode)
        return "mock"

    def allow_multiple_attempts(self) -> bool:
        mode = self.exam_mode()
        # practice/pyq default multi; mock/live may still allow via config
        default_multi = mode in ("practice", "pyq", "sectional")
        if "modes.allow_multiple_attempts" in str(self._rules):
            return bool(self.get("modes.allow_multiple_attempts", default_multi))
        return bool(self.get("modes.allow_multiple_attempts", True))

    def max_attempts_per_user(self) -> int:
        try:
            return max(0, int(self.get("modes.max_attempts_per_user", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def show_answer_after_each(self) -> bool:
        if self.exam_mode() == "practice":
            return bool(self.get("modes.show_answer_after_each", True))
        return bool(self.get("modes.show_answer_after_each", False))

    def show_result_immediately(self) -> bool:
        if self._exam is not None and hasattr(self._exam, "show_result_immediately"):
            # column wins when False for high-stakes; rules can still force True in practice
            col = bool(self._exam.show_result_immediately)
            rule = self.get("result.show_immediate")
            if rule is None:
                return col
            return bool(rule) and col if self.exam_mode() == "live" else bool(rule if rule is not None else col)
        return bool(self.get("result.show_immediate", True))

    def to_public_dict(self) -> Dict[str, Any]:
        """
        Safe rule snapshot for the exam player (no secrets).

        Frontend may ignore unknown keys — backward compatible additive payload.
        """
        return {
            "version": self.get("version", 1),
            "timing": {
                "overall_seconds": self.overall_seconds(),
                "auto_submit_on_expiry": self.auto_submit_on_expiry(),
                "allow_pause": self.allow_pause(),
                "max_pauses": self.max_pauses(),
                "section_timers": self.section_timers_enabled(),
                "section_auto_submit_on_expiry": self.section_auto_submit(),
                "warnings": self.get("timing.warnings") or [],
            },
            "sections": {
                "strict_order": self.strict_sections(),
                "lock_on_complete": self.lock_on_complete(),
                "allow_previous_section": self.allow_previous_section(),
                "allow_next_section": self.allow_next_section(),
                "show_section_tabs": bool(self.get("sections.show_section_tabs", True)),
            },
            "navigation": {
                "allow_skip": bool(self.get("navigation.allow_skip", True)),
                "allow_review": bool(self.get("navigation.allow_review", True)),
                "allow_mark_for_review": self.allow_mark_for_review(),
                "allow_clear_response": bool(self.get("navigation.allow_clear_response", True)),
                "free_question_navigation": self.free_question_navigation(),
                "resume_allowed": self.resume_allowed(),
            },
            "questions": {
                "shuffle_questions": self.shuffle_questions(),
                "shuffle_options": self.shuffle_options(),
                "mandatory_question_ids": self.mandatory_question_ids(),
                "optional_question_ids": self.optional_question_ids(),
            },
            "marking": {
                "negative_marking": self.negative_marking_enabled(),
                "default_marks": self.default_marks(),
                "default_negative_marks": self.default_negative_marks(),
                "partial_marking": self.partial_marking(),
            },
            "language": {
                "allowed": self.allowed_languages(),
                "default": self.default_language(),
                "allow_switch": self.allow_language_switch(),
            },
            "security": {
                "require_fullscreen": self.require_fullscreen(),
                "detect_tab_switch": self.detect_tab_switch(),
                "max_tab_switches": self.max_tab_switches(),
                "force_submit_on_max_tab_switches": self.force_submit_on_max_tabs(),
                "prevent_copy": bool(self.get("security.prevent_copy", True)),
                "prevent_paste": bool(self.get("security.prevent_paste", True)),
                "prevent_right_click": bool(self.get("security.prevent_right_click", True)),
            },
            "aids": {
                "calculator_allowed": self.calculator_allowed(),
                "rough_sheet_allowed": self.rough_sheet_allowed(),
            },
            "modes": {
                "exam_mode": self.exam_mode(),
                "show_answer_after_each": self.show_answer_after_each(),
                "show_solution_after_submit": bool(
                    self.get("modes.show_solution_after_submit", True)
                ),
                "allow_multiple_attempts": self.allow_multiple_attempts(),
                "max_attempts_per_user": self.max_attempts_per_user(),
            },
            "submission": {
                "confirm_before_submit": bool(self.get("submission.confirm_before_submit", True)),
                "warn_unanswered": bool(self.get("submission.warn_unanswered", True)),
                "warn_marked_review": bool(self.get("submission.warn_marked_review", True)),
                "auto_submit_on_expiry": self.auto_submit_on_expiry(),
            },
            "result": {
                "show_immediate": self.show_result_immediately(),
                "show_correct_answers": bool(self.get("result.show_correct_answers", True)),
                "show_explanations": bool(self.get("result.show_explanations", True)),
                "show_section_breakdown": bool(self.get("result.show_section_breakdown", True)),
                "show_time_analysis": bool(self.get("result.show_time_analysis", True)),
                "show_percentile": bool(self.get("result.show_percentile", True)),
                "show_rank_prediction": bool(self.get("result.show_rank_prediction", True)),
            },
            "ui": self.get("ui") or {},
        }


def apply_column_sync_to_rules(exam: Any, rules: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """
    When saving an exam, keep rules_json aligned with classic columns so
    configuration stays the single source of truth going forward.
    """
    merged = merge_exam_rules(rules if rules is not None else (
        exam.get_rules() if exam is not None and hasattr(exam, "get_rules") else {}
    ))
    if exam is None:
        return merged
    try:
        merged.setdefault("timing", {})["overall_seconds"] = int(exam.duration_seconds or 3600)
        merged.setdefault("sections", {})["strict_order"] = bool(exam.strict_sections)
        merged.setdefault("questions", {})["shuffle_questions"] = bool(exam.shuffle_questions)
        merged.setdefault("questions", {})["shuffle_options"] = bool(exam.shuffle_options)
        merged.setdefault("security", {})["require_fullscreen"] = bool(exam.require_fullscreen)
        merged.setdefault("security", {})["max_tab_switches"] = int(exam.max_tab_switches or 5)
        merged.setdefault("marking", {})["default_marks"] = float(exam.default_marks or 1)
        merged.setdefault("marking", {})["default_negative_marks"] = float(
            exam.default_negative_marks or 0
        )
        merged.setdefault("result", {})["show_immediate"] = bool(exam.show_result_immediately)
        merged.setdefault("modes", {})["exam_mode"] = exam.exam_mode or "mock"
    except Exception:
        logger.exception("apply_column_sync_to_rules failed")
    return merged
