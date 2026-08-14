"""User messages / feedback / test-request routes."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.user_message import UserMessage
from app.routes.admin import roles_required

logger = logging.getLogger("exam_os.routes.messages")

messages_bp = Blueprint("messages", __name__)


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _clip(value, max_len):
    if value is None:
        return None
    return str(value)[:max_len]


@messages_bp.post("")
@jwt_required()
def create_message():
    """User sends a test-request / feedback message."""
    data = _json_body()
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    mtype = (data.get("message_type") or "request").strip()[:32]
    if mtype not in ("request", "feedback", "other"):
        mtype = "request"

    try:
        user_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        user_id = None

    msg = UserMessage(
        user_id=user_id,
        name=_clip(data.get("name"), 120),
        email=_clip(data.get("email"), 200),
        message_type=mtype,
        message=_clip(message, 5000),
        status="new",
    )
    db.session.add(msg)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("create_message commit failed")
        return jsonify({"error": "Could not save message"}), 500
    return jsonify({"message": "Message sent. Thank you!", "item": msg.to_dict()}), 201


@messages_bp.get("/admin")
@roles_required("admin")
def list_messages():
    """Admin lists all user messages."""
    status = (request.args.get("status") or "").strip()[:32]
    mtype = (request.args.get("message_type") or "").strip()[:32]
    q = UserMessage.query
    if status:
        q = q.filter_by(status=status)
    if mtype:
        q = q.filter_by(message_type=mtype)
    items = q.order_by(UserMessage.id.desc()).all()
    return jsonify({
        "items": [m.to_dict() for m in items],
        "total": len(items),
    })


@messages_bp.post("/admin/<int:message_id>/status")
@roles_required("admin")
def update_message_status(message_id):
    """Admin marks a message read/done."""
    msg = UserMessage.query.get_or_404(message_id)
    data = _json_body()
    status = (data.get("status") or "").strip()[:32]
    if status not in ("new", "read", "done"):
        return jsonify({"error": "Invalid status"}), 400
    msg.status = status
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("update_message_status failed")
        return jsonify({"error": "Could not update"}), 500
    return jsonify({"message": "Updated", "item": msg.to_dict()})
