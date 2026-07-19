"""Subject CRUD routes.

Stable endpoints under ``/api/subjects``.
Admin write; any authenticated user may read active subjects for exam setup.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Final, Optional

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.extensions import db
from app.models.subject import Subject
from app.utils.decorators import roles_required
from app.utils.validators import require_fields

subjects_bp = Blueprint("subjects", __name__)
logger = logging.getLogger("exam_os.routes.subjects")

_MAX_NAME: Final[int] = 200
_MAX_CODE: Final[int] = 50
_MAX_ICON: Final[int] = 64
_MAX_COLOR: Final[int] = 32
_MAX_DESC: Final[int] = 5000
_MAX_SEARCH: Final[int] = 100
_COLOR_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _escape_like(term: str) -> str:
    """Escape ``%`` / ``_`` so user search cannot broaden LIKE unexpectedly."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_color(value: Any, default: str = "#1e40af") -> str:
    if value is None or value == "":
        return default
    text = str(value).strip()[:_MAX_COLOR]
    if not _COLOR_RE.match(text):
        return default
    return text if text.startswith("#") else f"#{text}"


@subjects_bp.get("")
@jwt_required()
def list_subjects():
    q = Subject.query
    if request.args.get("active_only", "true").lower() == "true":
        q = q.filter_by(is_active=True)
    search = (request.args.get("search") or "").strip()[:_MAX_SEARCH]
    if search:
        q = q.filter(Subject.name.ilike(f"%{_escape_like(search)}%", escape="\\"))
    items = q.order_by(Subject.order_index, Subject.name).all()
    return jsonify({"items": [s.to_dict() for s in items], "total": len(items)})


@subjects_bp.get("/<int:subject_id>")
@jwt_required()
def get_subject(subject_id):
    s = Subject.query.get_or_404(subject_id)
    data = s.to_dict()
    data["chapters"] = [c.to_dict() for c in s.chapters.order_by().all()]
    return jsonify(data)


@subjects_bp.post("")
@roles_required("admin")
def create_subject():
    data = _json_body()
    err = require_fields(data, ["name"])
    if err:
        return jsonify({"error": err}), 400

    name = str(data["name"]).strip()[:_MAX_NAME]
    if not name:
        return jsonify({"error": "name is required"}), 400
    if Subject.query.filter_by(name=name).first():
        return jsonify({"error": "Subject with this name already exists"}), 409

    code = (str(data.get("code") or "").strip()[:_MAX_CODE]) or None
    desc = data.get("description")
    if isinstance(desc, str):
        desc = desc[:_MAX_DESC]
    icon = str(data.get("icon") or "book").strip()[:_MAX_ICON] or "book"

    s = Subject(
        name=name,
        code=code,
        description=desc,
        icon=icon,
        color=_normalize_color(data.get("color")),
        order_index=_safe_int(data.get("order_index", 0)),
        is_active=bool(data.get("is_active", True)),
    )
    db.session.add(s)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_subject failed")
        return jsonify({"error": "Could not create subject"}), 500
    return jsonify({"message": "Subject created", "item": s.to_dict()}), 201


@subjects_bp.put("/<int:subject_id>")
@roles_required("admin")
def update_subject(subject_id):
    s = Subject.query.get_or_404(subject_id)
    data = _json_body()
    if "name" in data and data["name"]:
        name = str(data["name"]).strip()[:_MAX_NAME]
        if name:
            existing = Subject.query.filter(Subject.name == name, Subject.id != s.id).first()
            if existing:
                return jsonify({"error": "Subject name already taken"}), 409
            s.name = name
    if "code" in data:
        code = data["code"]
        s.code = (str(code).strip()[:_MAX_CODE] or None) if code not in (None, "") else None
    if "description" in data:
        desc = data["description"]
        s.description = str(desc)[:_MAX_DESC] if desc is not None else None
    if "icon" in data and data["icon"] is not None:
        s.icon = str(data["icon"]).strip()[:_MAX_ICON] or s.icon
    if "color" in data:
        s.color = _normalize_color(data.get("color"), default=s.color or "#1e40af")
    if "order_index" in data:
        s.order_index = _safe_int(data["order_index"], s.order_index or 0)
    if "is_active" in data:
        s.is_active = bool(data["is_active"])
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("update_subject id=%s failed", subject_id)
        return jsonify({"error": "Could not update subject"}), 500
    return jsonify({"message": "Subject updated", "item": s.to_dict()})


@subjects_bp.delete("/<int:subject_id>")
@roles_required("admin")
def delete_subject(subject_id):
    s = Subject.query.get_or_404(subject_id)
    db.session.delete(s)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_subject id=%s failed", subject_id)
        return jsonify({"error": "Could not delete subject"}), 500
    return jsonify({"message": "Subject deleted"})


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Soft-delete instead of hard delete when questions reference subject
# --------------------------------------------
