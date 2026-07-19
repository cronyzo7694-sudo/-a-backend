"""Notification engine models — queue, templates, user preferences, delivery logs.

Additive tables. Business code enqueues via NotificationEngine only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


class NotificationTemplate(db.Model):
    __tablename__ = "notification_templates"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    channel = db.Column(db.String(32), nullable=False, default="in_app")  # in_app|email|telegram|...
    subject_template = db.Column(db.String(255), nullable=True)
    body_template = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "channel": self.channel,
            "subject_template": self.subject_template,
            "body_template": self.body_template,
            "is_active": bool(self.is_active),
            "updated_at": _iso(self.updated_at),
        }


class NotificationPreference(db.Model):
    __tablename__ = "notification_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    # channel toggles
    in_app = db.Column(db.Boolean, default=True, nullable=False)
    email = db.Column(db.Boolean, default=True, nullable=False)
    telegram = db.Column(db.Boolean, default=False, nullable=False)
    push = db.Column(db.Boolean, default=True, nullable=False)
    whatsapp = db.Column(db.Boolean, default=False, nullable=False)
    sms = db.Column(db.Boolean, default=False, nullable=False)
    # category toggles
    exam_alerts = db.Column(db.Boolean, default=True, nullable=False)
    result_alerts = db.Column(db.Boolean, default=True, nullable=False)
    reminders = db.Column(db.Boolean, default=True, nullable=False)
    marketing = db.Column(db.Boolean, default=False, nullable=False)
    system = db.Column(db.Boolean, default=True, nullable=False)
    security = db.Column(db.Boolean, default=True, nullable=False)
    # optional provider handles
    telegram_chat_id = db.Column(db.String(64), nullable=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "channels": {
                "in_app": bool(self.in_app),
                "email": bool(self.email),
                "telegram": bool(self.telegram),
                "push": bool(self.push),
                "whatsapp": bool(self.whatsapp),
                "sms": bool(self.sms),
            },
            "categories": {
                "exam_alerts": bool(self.exam_alerts),
                "result_alerts": bool(self.result_alerts),
                "reminders": bool(self.reminders),
                "marketing": bool(self.marketing),
                "system": bool(self.system),
                "security": bool(self.security),
            },
            "telegram_chat_id": self.telegram_chat_id,
            "updated_at": _iso(self.updated_at),
        }


class Notification(db.Model):
    """In-app notification + queue row."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    # null user_id = broadcast target resolved into per-user rows or role fanout
    category = db.Column(db.String(64), nullable=False, default="system", index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    data_json = db.Column(db.Text, nullable=True)
    priority = db.Column(db.String(16), default="normal", nullable=False)  # low|normal|high
    status = db.Column(db.String(32), default="queued", nullable=False, index=True)
    # queued|processing|sent|partial|failed|read|cancelled
    channels_json = db.Column(db.Text, nullable=True)  # ["in_app","email"]
    scheduled_at = db.Column(db.DateTime, nullable=True, index=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    read_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.String(512), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    def get_data(self) -> Dict[str, Any]:
        try:
            d = json.loads(self.data_json or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def get_channels(self) -> List[str]:
        try:
            d = json.loads(self.channels_json or "[]")
            return [str(x) for x in d] if isinstance(d, list) else ["in_app"]
        except Exception:
            return ["in_app"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "category": self.category,
            "title": self.title,
            "body": self.body,
            "data": self.get_data(),
            "priority": self.priority,
            "status": self.status,
            "channels": self.get_channels(),
            "scheduled_at": _iso(self.scheduled_at),
            "sent_at": _iso(self.sent_at),
            "read_at": _iso(self.read_at),
            "error_message": self.error_message,
            "created_at": _iso(self.created_at),
            "is_read": self.read_at is not None,
        }


class NotificationDelivery(db.Model):
    __tablename__ = "notification_deliveries"

    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(
        db.Integer, db.ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider = db.Column(db.String(32), nullable=False)  # in_app|smtp|telegram|fcm|...
    status = db.Column(db.String(32), default="pending", nullable=False, index=True)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.String(512), nullable=True)
    provider_ref = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "notification_id": self.notification_id,
            "provider": self.provider,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "provider_ref": self.provider_ref,
            "updated_at": _iso(self.updated_at),
        }
