"""Chapter CRUD routes under ``/api/chapters``."""

from __future__ import annotations

import logging
from typing import Any, Dict, Final

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.extensions import db
from app.models.chapter import Chapter
from app.models.subject import Subject
from app.utils.decorators import roles_required
from app.utils.validators import require_fields

chapters_bp = Blueprint("chapters", __name__)
logger = logging.getLogger("exam_os.routes.chapters")

_MAX_NAME: Final[int] = 200
_MAX_DESC: Final[int] = 5000
_MAX_SEARCH: Final[int] = 100


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _safe_int(value: Any, default: int = 0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@chapters_bp.get("")
@jwt_required()
def list_chapters():
    q = Chapter.query
    subject_id = request.args.get("subject_id")
    if subject_id not in (None, ""):
        try:
            q = q.filter_by(subject_id=int(subject_id))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid subject_id"}), 400
    if request.args.get("active_only", "true").lower() == "true":
        q = q.filter_by(is_active=True)
    search = (request.args.get("search") or "").strip()[:_MAX_SEARCH]
    if search:
        q = q.filter(Chapter.name.ilike(f"%{_escape_like(search)}%", escape="\\"))
    items = q.order_by(Chapter.order_index, Chapter.name).all()
    return jsonify({"items": [c.to_dict() for c in items], "total": len(items)})


@chapters_bp.get("/<int:chapter_id>")
@jwt_required()
def get_chapter(chapter_id):
    c = Chapter.query.get_or_404(chapter_id)
    return jsonify(c.to_dict())


@chapters_bp.post("")
@roles_required("admin")
def create_chapter():
    data = _json_body()
    err = require_fields(data, ["name", "subject_id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        sid = int(data["subject_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid subject_id"}), 400
    subject = Subject.query.get(sid)
    if not subject:
        return jsonify({"error": "Subject not found"}), 404
    name = str(data["name"]).strip()[:_MAX_NAME]
    if not name:
        return jsonify({"error": "name is required"}), 400
    if Chapter.query.filter_by(subject_id=subject.id, name=name).first():
        return jsonify({"error": "Chapter already exists in this subject"}), 409
    desc = data.get("description")
    if isinstance(desc, str):
        desc = desc[:_MAX_DESC]
    c = Chapter(
        subject_id=subject.id,
        name=name,
        description=desc,
        order_index=_safe_int(data.get("order_index", 0)),
        is_active=bool(data.get("is_active", True)),
    )
    db.session.add(c)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_chapter failed")
        return jsonify({"error": "Could not create chapter"}), 500
    return jsonify({"message": "Chapter created", "item": c.to_dict()}), 201


@chapters_bp.put("/<int:chapter_id>")
@roles_required("admin")
def update_chapter(chapter_id):
    c = Chapter.query.get_or_404(chapter_id)
    data = _json_body()
    if "name" in data and data["name"]:
        name = str(data["name"]).strip()[:_MAX_NAME]
        if name:
            c.name = name
    if "description" in data:
        desc = data["description"]
        c.description = str(desc)[:_MAX_DESC] if desc is not None else None
    if "order_index" in data:
        c.order_index = _safe_int(data["order_index"], c.order_index or 0)
    if "is_active" in data:
        c.is_active = bool(data["is_active"])
    if "subject_id" in data:
        try:
            sid = int(data["subject_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid subject_id"}), 400
        if not Subject.query.get(sid):
            return jsonify({"error": "Subject not found"}), 404
        c.subject_id = sid
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("update_chapter id=%s failed", chapter_id)
        return jsonify({"error": "Could not update chapter"}), 500
    return jsonify({"message": "Chapter updated", "item": c.to_dict()})


@chapters_bp.delete("/<int:chapter_id>")
@roles_required("admin")
def delete_chapter(chapter_id):
    c = Chapter.query.get_or_404(chapter_id)
    db.session.delete(c)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("delete_chapter id=%s failed", chapter_id)
        return jsonify({"error": "Could not delete chapter"}), 500
    return jsonify({"message": "Chapter deleted"})
