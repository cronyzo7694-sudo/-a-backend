"""Notification API — user inbox + admin broadcast/history.

Prefixes (additive)::
    /api/notifications/*
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.notification import Notification, NotificationDelivery, NotificationPreference, NotificationTemplate
from app.models.user import User
from app.services.feature_flags import is_enabled
from app.services.notification_engine import (
    broadcast,
    ensure_default_templates,
    get_or_create_preferences,
    notify,
    process_queue,
    provider_status,
)
from app.services.permission_engine import can
from app.utils.decorators import get_current_user, roles_required
from app.utils.validators import parse_pagination, require_fields

notifications_bp = Blueprint("notifications", __name__)
logger = logging.getLogger("exam_os.routes.notifications")


def _json() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _uid() -> int | None:
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


@notifications_bp.get("")
@jwt_required()
def list_mine():
    if not is_enabled("NOTIFICATIONS_ENABLED", True) and not is_enabled("ENABLE_NOTIFICATIONS", True):
        return jsonify({"items": [], "total": 0, "unread": 0})
    uid = _uid()
    if uid is None:
        return jsonify({"error": "Unauthorized"}), 401
    page, per_page = parse_pagination(request.args)
    q = Notification.query.filter_by(user_id=uid).order_by(Notification.id.desc())
    total = q.count()
    items = q.offset((page - 1) * per_page).limit(per_page).all()
    unread = Notification.query.filter_by(user_id=uid, read_at=None).filter(
        Notification.status.in_(("sent", "queued", "partial", "processing"))
    ).count()
    return jsonify({
        "items": [n.to_dict() for n in items],
        "total": total,
        "unread": unread,
        "page": page,
        "per_page": per_page,
    })


@notifications_bp.get("/unread-count")
@jwt_required()
def unread_count():
    uid = _uid()
    if uid is None:
        return jsonify({"unread": 0})
    n = Notification.query.filter_by(user_id=uid, read_at=None).count()
    return jsonify({"unread": n})


@notifications_bp.post("/<int:notification_id>/read")
@jwt_required()
def mark_read(notification_id: int):
    uid = _uid()
    n = Notification.query.filter_by(id=notification_id, user_id=uid).first_or_404()
    if not n.read_at:
        from app.models.notification import utcnow

        n.read_at = utcnow()
        if n.status in ("sent", "queued", "partial"):
            n.status = "read"
        db.session.commit()
    return jsonify({"message": "ok", "item": n.to_dict()})


@notifications_bp.post("/read-all")
@jwt_required()
def mark_all_read():
    uid = _uid()
    from app.models.notification import utcnow

    now = utcnow()
    rows = Notification.query.filter_by(user_id=uid, read_at=None).all()
    for n in rows:
        n.read_at = now
        n.status = "read"
    db.session.commit()
    return jsonify({"message": "ok", "count": len(rows)})


@notifications_bp.get("/preferences")
@jwt_required()
def get_prefs():
    uid = _uid()
    if uid is None:
        return jsonify({"error": "Unauthorized"}), 401
    pref = get_or_create_preferences(uid)
    db.session.commit()
    return jsonify(pref.to_dict())


@notifications_bp.put("/preferences")
@jwt_required()
def update_prefs():
    uid = _uid()
    if uid is None:
        return jsonify({"error": "Unauthorized"}), 401
    pref = get_or_create_preferences(uid)
    data = _json()
    channels = data.get("channels") if isinstance(data.get("channels"), dict) else {}
    categories = data.get("categories") if isinstance(data.get("categories"), dict) else {}
    for key in ("in_app", "email", "telegram", "push", "whatsapp", "sms"):
        if key in channels:
            setattr(pref, key, bool(channels[key]))
    for key in ("exam_alerts", "result_alerts", "reminders", "marketing", "system", "security"):
        if key in categories:
            setattr(pref, key, bool(categories[key]))
    if "telegram_chat_id" in data:
        val = data.get("telegram_chat_id")
        pref.telegram_chat_id = str(val).strip()[:64] if val else None
    # Security channel cannot be fully disabled for safety — keep True if they try False
    if categories.get("security") is False:
        pref.security = True
    db.session.commit()
    return jsonify({"message": "Preferences saved", "item": pref.to_dict()})


# ----- Admin -----


@notifications_bp.get("/admin/status")
@roles_required("admin")
def admin_status():
    ensure_default_templates()
    return jsonify(provider_status())


@notifications_bp.get("/admin/templates")
@roles_required("admin")
def list_templates():
    ensure_default_templates()
    items = NotificationTemplate.query.order_by(NotificationTemplate.code).all()
    return jsonify({"items": [t.to_dict() for t in items], "total": len(items)})


@notifications_bp.put("/admin/templates/<int:template_id>")
@roles_required("admin")
def update_template(template_id: int):
    t = NotificationTemplate.query.get_or_404(template_id)
    data = _json()
    if "subject_template" in data:
        t.subject_template = str(data.get("subject_template") or "")[:255]
    if "body_template" in data and data.get("body_template") is not None:
        t.body_template = str(data.get("body_template"))[:20000]
    if "is_active" in data:
        t.is_active = bool(data["is_active"])
    if "channel" in data and data["channel"]:
        t.channel = str(data["channel"])[:32]
    db.session.commit()
    return jsonify({"message": "Template updated", "item": t.to_dict()})


@notifications_bp.post("/admin/broadcast")
@roles_required("admin")
def admin_broadcast():
    if not is_enabled("ENABLE_ADMIN_BROADCAST", True):
        return jsonify({"error": "Broadcast disabled by configuration"}), 403
    data = _json()
    err = require_fields(data, ["title", "body"])
    if err:
        return jsonify({"error": err}), 400
    channels = data.get("channels")
    if channels is not None and not isinstance(channels, list):
        return jsonify({"error": "channels must be a list"}), 400
    user_ids = data.get("user_ids")
    if user_ids is not None and not isinstance(user_ids, list):
        return jsonify({"error": "user_ids must be a list"}), 400
    role = data.get("role")
    if role and role not in ("student", "guest", "admin"):
        return jsonify({"error": "Invalid role"}), 400

    admin = get_current_user()
    count = broadcast(
        title=str(data["title"])[:255],
        body=str(data["body"])[:20000],
        category=str(data.get("category") or "admin_broadcast")[:64],
        role=role,
        user_ids=[int(x) for x in (user_ids or []) if str(x).isdigit()][:2000],
        channels=channels,
        created_by=admin.id if admin else None,
    )
    process_queue(limit=100)
    return jsonify({"message": "Broadcast enqueued", "recipients": count}), 201


@notifications_bp.post("/admin/send")
@roles_required("admin")
def admin_send_one():
    data = _json()
    err = require_fields(data, ["user_id", "title", "body"])
    if err:
        return jsonify({"error": err}), 400
    try:
        uid = int(data["user_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user_id"}), 400
    if not User.query.get(uid):
        return jsonify({"error": "User not found"}), 404
    admin = get_current_user()
    n = notify(
        user_id=uid,
        category=str(data.get("category") or "custom")[:64],
        title=str(data["title"])[:255],
        body=str(data["body"])[:20000],
        channels=data.get("channels") if isinstance(data.get("channels"), list) else ["in_app"],
        created_by=admin.id if admin else None,
        data=data.get("data") if isinstance(data.get("data"), dict) else {},
    )
    if not n:
        return jsonify({"error": "Could not enqueue"}), 400
    return jsonify({"message": "Queued", "item": n.to_dict()}), 201


@notifications_bp.get("/admin/history")
@roles_required("admin")
def admin_history():
    page, per_page = parse_pagination(request.args)
    q = Notification.query.order_by(Notification.id.desc())
    if request.args.get("status"):
        q = q.filter_by(status=request.args["status"][:32])
    if request.args.get("category"):
        q = q.filter_by(category=request.args["category"][:64])
    total = q.count()
    items = q.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        "items": [n.to_dict() for n in items],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@notifications_bp.post("/admin/process-queue")
@roles_required("admin")
def admin_process_queue():
    n = process_queue(limit=int(request.args.get("limit") or 100))
    return jsonify({"message": "ok", "processed": n})


@notifications_bp.post("/admin/<int:notification_id>/retry")
@roles_required("admin")
def admin_retry(notification_id: int):
    n = Notification.query.get_or_404(notification_id)
    for d in NotificationDelivery.query.filter_by(notification_id=n.id).all():
        if d.status == "failed":
            d.status = "pending"
            d.last_error = None
    n.status = "queued"
    n.error_message = None
    db.session.commit()
    processed = process_queue(limit=20, notification_id=n.id)
    return jsonify({"message": "retry queued", "processed": processed, "item": n.to_dict()})
