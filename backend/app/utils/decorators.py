"""Auth / role decorators for Exam OS CBT API routes.

Public contract (stable — used across ``app.routes.*``)::

    @roles_required("admin")
    @roles_required("admin", "student")

    user = get_current_user()  # Optional[User]

Security:
    * JWT must be present and valid before role checks run.
    * Role is taken from verified JWT claims (not request body).
    * ``get_current_user`` refuses inactive accounts and invalid identities.
    * Never leaks stack traces or internal claim dumps to clients.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, cast

from flask import jsonify
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request

from app.models.user import User

logger = logging.getLogger("exam_os.utils.decorators")

F = TypeVar("F", bound=Callable[..., Any])

# Known CBT roles — keep aligned with User.role vocabulary
_KNOWN_ROLES = frozenset({"admin", "student"})


def _forbidden_response():
    return (
        jsonify({
            "error": "Forbidden",
            "message": "Insufficient permissions",
        }),
        403,
    )


def _unauthorized_response(message: str = "Authentication required"):
    return (
        jsonify({
            "error": "Unauthorized",
            "message": message,
        }),
        401,
    )


def roles_required(*roles: str) -> Callable[[F], F]:
    """
    Require a valid JWT whose ``role`` claim is one of ``roles``.

    Usage::

        @roles_required("admin")
        def create_exam(): ...

    Empty ``roles`` is treated as deny-all (misconfiguration fail-closed).
    """

    allowed = tuple(r for r in roles if isinstance(r, str) and r)
    allowed_set = frozenset(allowed)

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any):
            # Raises / aborts via flask-jwt-extended on missing/invalid token
            verify_jwt_in_request()

            if not allowed_set:
                logger.error(
                    "roles_required used with no roles on %s — denying",
                    getattr(fn, "__name__", "?"),
                )
                return _forbidden_response()

            try:
                claims = get_jwt() or {}
            except Exception:  # noqa: BLE001
                logger.exception("get_jwt failed in roles_required")
                return _unauthorized_response()

            role = claims.get("role")
            if not isinstance(role, str) or role not in allowed_set:
                return _forbidden_response()

            # --------------------------------------------
            # EXTENSION POINT: optional live DB role re-check
            # (detect role revoked after token issuance)
            # --------------------------------------------

            return fn(*args, **kwargs)

        return cast(F, wrapper)

    return decorator


def get_current_user() -> Optional[User]:
    """
    Load the ``User`` row for the current JWT identity.

    Returns
    -------
    User or None
        ``None`` when the token identity is malformed, the user is missing,
        or the account is deactivated. Callers should return 401/404.
    """
    try:
        verify_jwt_in_request()
    except Exception:  # noqa: BLE001 — caller decides response shape
        return None

    user_id = get_jwt_identity()
    try:
        uid = int(user_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.info("get_current_user: non-integer identity %r", user_id)
        return None

    if uid < 1:
        return None

    try:
        user = User.query.get(uid)
    except Exception:  # noqa: BLE001
        logger.exception("get_current_user query failed id=%s", uid)
        return None

    if user is None:
        return None

    # Deactivated accounts must not act as authenticated principals mid-exam
    if not getattr(user, "is_active", True):
        return None

    return user


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - roles_required(fresh=True) / permission bitmasks beyond role string
# - Optional DB role revalidation + short-lived claim cache
# - get_current_user_id() helper to avoid full row load on hot paths
# --------------------------------------------
