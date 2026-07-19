"""Maintenance / emergency mode engine."""

from __future__ import annotations

from typing import Any, Optional, Set, Tuple

from app.services.config_engine import get_config


def is_maintenance_enabled() -> bool:
    cfg = get_config()
    return bool(cfg.get("maintenance.enabled") or cfg.feature("ENABLE_MAINTENANCE_MODE"))


def is_read_only() -> bool:
    return bool(get_config().get("maintenance.read_only"))


def is_admin_only() -> bool:
    return bool(get_config().get("maintenance.admin_only"))


def is_emergency_lock() -> bool:
    return bool(get_config().get("maintenance.emergency_lock"))


def allowed_paths() -> Set[str]:
    raw = get_config().get("maintenance.allow_paths") or []
    if not isinstance(raw, list):
        return {"/api/health"}
    return {str(p) for p in raw}


def maintenance_message() -> str:
    return str(
        get_config().get("maintenance.message")
        or "We are undergoing scheduled maintenance. Please try again shortly."
    )


def check_request_allowed(path: str, method: str, user: Any = None) -> Tuple[bool, Optional[str]]:
    """
    Return (allowed, error_message).
    Safe paths always allowed. Admin bypass when admin_only / maintenance.
    """
    path = path or ""
    method = (method or "GET").upper()

    if path in allowed_paths() or path.startswith("/api/health"):
        return True, None

    role = getattr(user, "role", None) if user is not None else None

    if is_emergency_lock():
        if role == "admin":
            return True, None
        return False, maintenance_message()

    if is_maintenance_enabled():
        if role == "admin":
            return True, None
        if is_admin_only() and role != "admin":
            return False, maintenance_message()
        # During maintenance, block mutating methods for non-admins
        if method in ("POST", "PUT", "PATCH", "DELETE") and role != "admin":
            return False, maintenance_message()
        if role != "admin":
            # Optionally block all non-allowlisted traffic
            return False, maintenance_message()

    if is_read_only() and method in ("POST", "PUT", "PATCH", "DELETE"):
        if role == "admin":
            return True, None
        return False, "Platform is in read-only mode"

    return True, None
