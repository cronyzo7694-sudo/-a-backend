"""Admin utility routes — user management under ``/api/admin``."""

from __future__ import annotations

import logging
from typing import Any, Dict, Final

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.extensions import db
from app.models.user import User
from app.utils.decorators import roles_required
from app.utils.validators import (
    is_strong_password,
    is_valid_email,
    parse_pagination,
    require_fields,
)

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger("exam_os.routes.admin")

_MAX_SEARCH: Final[int] = 100
_MAX_NAME: Final[int] = 150
_MAX_PASSWORD: Final[int] = 256
_ALLOWED_ROLES = frozenset({"admin", "student"})


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@admin_bp.get("/users")
@roles_required("admin")
def list_users():
    page, per_page = parse_pagination(request.args)
    q = User.query
    role = request.args.get("role")
    if role:
        if role not in _ALLOWED_ROLES:
            return jsonify({"error": "Invalid role filter"}), 400
        q = q.filter_by(role=role)
    search = (request.args.get("search") or "").strip()[:_MAX_SEARCH]
    if search:
        like = f"%{_escape_like(search)}%"
        q = q.filter(
            db.or_(
                User.email.ilike(like, escape="\\"),
                User.full_name.ilike(like, escape="\\"),
            )
        )
    total = q.count()
    items = (
        q.order_by(User.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return jsonify({
        "items": [u.to_dict() for u in items],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@admin_bp.post("/users")
@roles_required("admin")
def create_user():
    data = _json_body()
    err = require_fields(data, ["email", "password", "full_name"])
    if err:
        return jsonify({"error": err}), 400
    email = str(data["email"]).strip().lower()[:255]
    if not is_valid_email(email):
        return jsonify({"error": "Invalid email"}), 400
    password = data.get("password")
    if not isinstance(password, str) or len(password) > _MAX_PASSWORD:
        return jsonify({"error": "Invalid password"}), 400
    ok, msg = is_strong_password(password)
    if not ok:
        return jsonify({"error": msg}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email already exists"}), 409
    full_name = str(data["full_name"]).strip()[:_MAX_NAME]
    if not full_name:
        return jsonify({"error": "full_name is required"}), 400
    role = data.get("role", "student")
    if role not in _ALLOWED_ROLES:
        role = "student"
    user = User(email=email, full_name=full_name, role=role)
    try:
        user.set_password(password)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc) or "Invalid password"}), 400
    db.session.add(user)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_user failed")
        return jsonify({"error": "Could not create user"}), 500
    return jsonify({"message": "User created", "item": user.to_dict()}), 201


@admin_bp.put("/users/<int:user_id>")
@roles_required("admin")
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    data = _json_body()
    actor_id = None
    try:
        actor_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        actor_id = None

    if "full_name" in data and data["full_name"]:
        name = str(data["full_name"]).strip()[:_MAX_NAME]
        if name:
            user.full_name = name
    if "role" in data and data["role"] in _ALLOWED_ROLES:
        # Prevent self-demotion lockout of the last admin
        if actor_id == user.id and data["role"] != "admin" and user.role == "admin":
            other_admins = User.query.filter(
                User.role == "admin",
                User.id != user.id,
                User.is_active.is_(True),
            ).count()
            if other_admins < 1:
                return jsonify({"error": "Cannot demote the last active admin"}), 400
        user.role = data["role"]
    if "is_active" in data:
        new_active = bool(data["is_active"])
        if actor_id == user.id and not new_active:
            return jsonify({"error": "Cannot deactivate your own account"}), 400
        if (
            user.role == "admin"
            and user.is_active
            and not new_active
        ):
            other_admins = User.query.filter(
                User.role == "admin",
                User.id != user.id,
                User.is_active.is_(True),
            ).count()
            if other_admins < 1:
                return jsonify({"error": "Cannot deactivate the last active admin"}), 400
        user.is_active = new_active
    if data.get("password"):
        password = data["password"]
        if not isinstance(password, str) or len(password) > _MAX_PASSWORD:
            return jsonify({"error": "Invalid password"}), 400
        ok, msg = is_strong_password(password)
        if not ok:
            return jsonify({"error": msg}), 400
        try:
            user.set_password(password)
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc) or "Invalid password"}), 400
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("update_user id=%s failed", user_id)
        return jsonify({"error": "Could not update user"}), 500
    return jsonify({"message": "User updated", "item": user.to_dict()})


@admin_bp.delete("/users/<int:user_id>")
@roles_required("admin")
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    try:
        actor_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        actor_id = None
    if actor_id == user.id:
        return jsonify({"error": "Cannot delete your own account"}), 400
    if user.role == "admin":
        other_admins = User.query.filter(
            User.role == "admin",
            User.id != user.id,
            User.is_active.is_(True),
        ).count()
        if other_admins < 1:
            return jsonify({"error": "Cannot delete the last active admin"}), 400
    db.session.delete(user)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_user id=%s failed", user_id)
        return jsonify({"error": "Could not delete user"}), 500
    return jsonify({"message": "User deleted"})


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Soft-delete users and anonymize PII for GDPR
# - Audit log table for admin mutations
# --------------------------------------------
