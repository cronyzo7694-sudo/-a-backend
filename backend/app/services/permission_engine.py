"""Permission Engine — single gateway for access decisions.

Exam Engine and routes must ask here instead of hardcoding role/plan checks.

Public::

    can(user, permission, context=None) -> bool
    assert_can(user, permission, context=None) -> None | (json, status)
    check_exam_access(user, exam) -> (ok: bool, reason: str)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from app.services.config_engine import get_config
from app.services.feature_flags import is_enabled

logger = logging.getLogger("exam_os.services.permission_engine")

# Permission vocabulary (stable strings)
PERM_TAKE_EXAM = "exam.take"
PERM_RESUME_EXAM = "exam.resume"
PERM_VIEW_ANALYTICS = "analytics.view"
PERM_VIEW_AI_COACH = "ai_coach.view"
PERM_IMPORT_QUESTIONS = "import.questions"
PERM_EXPORT_PDF = "export.pdf"
PERM_MANAGE_USERS = "admin.users"
PERM_MANAGE_EXAMS = "admin.exams"
PERM_MANAGE_BANK = "admin.bank"
PERM_ADMIN_PANEL = "admin.panel"
PERM_PREMIUM_CONTENT = "content.premium"
PERM_USE_WALLET = "wallet.use"
PERM_VIEW_LEADERBOARD = "leaderboard.view"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _role(user: Any) -> str:
    if user is None:
        return "guest"
    return str(getattr(user, "role", None) or "student")


def _is_admin(user: Any) -> bool:
    return _role(user) == "admin"


def _is_active(user: Any) -> bool:
    if user is None:
        return False
    return bool(getattr(user, "is_active", True))


def get_user_entitlements(user: Any) -> Dict[str, Any]:
    """
    Resolve subscription / plan entitlements for a user.

    Uses DB models when present; otherwise free-tier defaults from config.
    """
    cfg = get_config()
    mode = (cfg.get("monetization.mode") or "free").lower()
    base = {
        "plan_code": "free",
        "is_premium": False,
        "features": set(["exams.practice", "analytics.basic"]),
        "status": "active",
        "expires_at": None,
    }

    if not is_enabled("ENABLE_SUBSCRIPTIONS") and mode in ("free", "ads"):
        # Everyone is free-tier; premium false
        if mode == "free":
            base["features"] = set(["*"])  # open platform
            base["is_premium"] = False
        return base

    # Try DB subscription
    try:
        from app.models.platform import Subscription

        if user is not None and getattr(user, "id", None):
            sub = (
                Subscription.query.filter_by(user_id=user.id, status="active")
                .order_by(Subscription.id.desc())
                .first()
            )
            if sub is not None:
                # grace period
                grace = int(cfg.get("monetization.grace_period_days") or 0)
                exp = sub.expires_at
                if exp is not None and exp < _utcnow():
                    from datetime import timedelta

                    if exp + timedelta(days=grace) < _utcnow():
                        pass  # expired
                    else:
                        base["status"] = "grace"
                        base["plan_code"] = sub.plan_code
                        base["is_premium"] = sub.plan_code not in ("free", "")
                        base["features"] = set(sub.feature_list())
                        base["expires_at"] = exp
                        return base
                else:
                    base["plan_code"] = sub.plan_code or "free"
                    base["is_premium"] = base["plan_code"] not in ("free", "")
                    base["features"] = set(sub.feature_list())
                    base["expires_at"] = exp
                    base["status"] = "active"
                    return base
    except Exception:
        logger.debug("subscription lookup skipped", exc_info=True)

    # Trial
    if is_enabled("ENABLE_TRIAL") and user is not None:
        try:
            created = getattr(user, "created_at", None)
            trial_days = int(cfg.get("monetization.trial_days") or 0)
            if created and trial_days > 0:
                from datetime import timedelta

                if created + timedelta(days=trial_days) >= _utcnow():
                    base["plan_code"] = "trial"
                    base["is_premium"] = True
                    base["features"] = set(["exams.*", "analytics.*", "ai_coach"])
                    base["status"] = "trial"
                    return base
        except Exception:
            pass

    return base


def _feature_match(granted: set, needed: str) -> bool:
    if "*" in granted:
        return True
    if needed in granted:
        return True
    # wildcard prefix exams.*
    for g in granted:
        if g.endswith(".*") and needed.startswith(g[:-1]):
            return True
    return False


def can(user: Any, permission: str, context: Optional[Mapping[str, Any]] = None) -> bool:
    """Return True if user may perform permission under optional context."""
    context = context or {}
    cfg = get_config()

    # Global emergency lock — only admin
    if cfg.get("maintenance.emergency_lock"):
        return _is_admin(user) and _is_active(user)

    if user is not None and not _is_active(user):
        return False

    role = _role(user)
    ent = get_user_entitlements(user)

    # Admin bypass for operational permissions
    if role == "admin":
        if permission.startswith("admin.") or permission in (
            PERM_IMPORT_QUESTIONS,
            PERM_MANAGE_EXAMS,
            PERM_MANAGE_BANK,
            PERM_MANAGE_USERS,
            PERM_ADMIN_PANEL,
            PERM_TAKE_EXAM,
            PERM_VIEW_ANALYTICS,
            PERM_VIEW_AI_COACH,
            PERM_EXPORT_PDF,
        ):
            if permission == PERM_ADMIN_PANEL and not is_enabled("ENABLE_ADMIN_PANEL", True):
                return False
            return True

    # Feature-flag gates
    flag_map = {
        PERM_VIEW_ANALYTICS: "ENABLE_ANALYTICS",
        PERM_VIEW_AI_COACH: "ENABLE_AI_COACH",
        PERM_IMPORT_QUESTIONS: "ENABLE_IMPORT",
        PERM_EXPORT_PDF: "ENABLE_PDF_EXPORT",
        PERM_VIEW_LEADERBOARD: "ENABLE_LEADERBOARD",
        PERM_USE_WALLET: "ENABLE_WALLET",
        PERM_ADMIN_PANEL: "ENABLE_ADMIN_PANEL",
    }
    flag = flag_map.get(permission)
    if flag and not is_enabled(flag, default=True):
        return False

    # Role baselines
    if permission == PERM_TAKE_EXAM:
        # Anonymous / guest sessions (no email signup) — config-driven
        if role == "guest":
            if not is_enabled("ENABLE_GUEST_ACCESS", True):
                return False
            exam = context.get("exam")
            # Guests: published practice/mock/pyq only (not live high-stakes unless open platform)
            if exam is not None and getattr(exam, "exam_mode", None) == "live":
                mode = (cfg.get("monetization.mode") or "free").lower()
                return mode == "free" and not is_enabled("ENABLE_SUBSCRIPTIONS")
            return True
        if role not in ("student", "admin", "guest"):
            return False
        # subscription mode may require plan feature
        mode = (cfg.get("monetization.mode") or "free").lower()
        if mode in ("subscription", "hybrid") and is_enabled("ENABLE_SUBSCRIPTIONS"):
            exam = context.get("exam")
            if exam is not None and getattr(exam, "exam_mode", None) == "live":
                return _feature_match(ent["features"], "exams.live") or _feature_match(
                    ent["features"], "exams.*"
                )
            return (
                _feature_match(ent["features"], "exams.practice")
                or _feature_match(ent["features"], "exams.*")
                or _feature_match(ent["features"], "exams.mock")
                or ent["plan_code"] in ("free", "trial")
                or not is_enabled("ENABLE_SUBSCRIPTIONS")
            )
        return True

    if permission == PERM_RESUME_EXAM:
        return can(user, PERM_TAKE_EXAM, context)

    if permission == PERM_VIEW_ANALYTICS:
        if not is_enabled("ENABLE_ANALYTICS", True):
            return False
        if role in ("student", "admin", "guest"):
            mode = (cfg.get("monetization.mode") or "free").lower()
            if mode in ("subscription", "hybrid") and is_enabled("ENABLE_SUBSCRIPTIONS"):
                if role == "guest":
                    return True  # basic own-attempt stats only at route layer
                return _feature_match(ent["features"], "analytics.basic") or _feature_match(
                    ent["features"], "analytics.*"
                ) or ent["is_premium"] or ent["plan_code"] == "free"
            return True
        return False

    if permission == PERM_VIEW_AI_COACH:
        if not is_enabled("ENABLE_AI_COACH", True):
            return False
        if role == "admin":
            return True
        if is_enabled("ENABLE_SUBSCRIPTIONS") and (cfg.get("monetization.mode") or "") in (
            "subscription",
            "hybrid",
        ):
            return _feature_match(ent["features"], "ai_coach") or ent["is_premium"]
        return True

    if permission == PERM_IMPORT_QUESTIONS:
        return role == "admin" and is_enabled("ENABLE_IMPORT", True)

    if permission == PERM_EXPORT_PDF:
        return is_enabled("ENABLE_PDF_EXPORT") and (
            role == "admin" or ent["is_premium"] or _feature_match(ent["features"], "export")
        )

    if permission == PERM_PREMIUM_CONTENT:
        return ent["is_premium"] or role == "admin"

    if permission in (PERM_MANAGE_USERS, PERM_MANAGE_EXAMS, PERM_MANAGE_BANK, PERM_ADMIN_PANEL):
        return role == "admin"

    if permission == PERM_VIEW_LEADERBOARD:
        return is_enabled("ENABLE_LEADERBOARD") and role != "guest"

    if permission == PERM_USE_WALLET:
        return is_enabled("ENABLE_WALLET") and role != "guest"

    return False


def check_exam_access(user: Any, exam: Any) -> Tuple[bool, str]:
    """Exam Engine entry: can this user access this exam?"""
    if exam is None:
        return False, "Exam not found"
    if user is None:
        return False, "Authentication required"
    if not _is_active(user):
        return False, "Account deactivated"

    # Published gate for students (admins always)
    if _role(user) != "admin" and getattr(exam, "status", None) != "published":
        return False, "Exam is not published"

    if not can(user, PERM_TAKE_EXAM, {"exam": exam}):
        return False, "Subscription or plan does not allow this exam"

    # Daily limits from monetization config (0 = unlimited)
    cfg = get_config()
    daily_limit = int(cfg.get("monetization.free_daily_exam_limit") or 0)
    ent = get_user_entitlements(user)
    if daily_limit > 0 and not ent["is_premium"] and _role(user) != "admin":
        try:
            from app.models.attempt import Attempt

            start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            count = Attempt.query.filter(
                Attempt.user_id == user.id,
                Attempt.started_at >= start,
            ).count()
            if count >= daily_limit:
                return False, "Daily exam limit reached"
        except Exception:
            logger.debug("daily limit check skipped", exc_info=True)

    return True, "ok"


def permission_denied_response(message: str = "Insufficient permissions"):
    from flask import jsonify

    return jsonify({"error": "Forbidden", "message": message}), 403
