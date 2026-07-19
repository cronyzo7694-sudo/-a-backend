"""Enterprise Notification Engine — queue + pluggable providers.

Business code calls only::

    from app.services.notification_engine import notify, process_queue

Never import Telegram/SMTP SDKs from routes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.extensions import db
from app.models.notification import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
    utcnow,
)
from app.models.user import User
from app.services.feature_flags import is_enabled
from app.services.config_engine import get_setting

logger = logging.getLogger("exam_os.services.notification")

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# category → preference field
_CATEGORY_PREF = {
    "exam_scheduled": "exam_alerts",
    "exam_reminder": "exam_alerts",
    "exam_started": "exam_alerts",
    "exam_ending": "exam_alerts",
    "exam_submitted": "exam_alerts",
    "result_published": "result_alerts",
    "daily_reminder": "reminders",
    "revision_reminder": "reminders",
    "weak_topic_reminder": "reminders",
    "practice_reminder": "reminders",
    "announcement": "system",
    "admin_broadcast": "system",
    "maintenance": "system",
    "system_alert": "system",
    "security_alert": "security",
    "password_changed": "security",
    "login_alert": "security",
    "payment_success": "system",
    "payment_failed": "system",
    "subscription_expiry": "system",
    "coupon": "marketing",
    "referral": "marketing",
    "custom": "system",
}

DEFAULT_TEMPLATES: Dict[str, Dict[str, str]] = {
    "exam_reminder": {
        "subject": "Reminder: {{exam}} starts soon",
        "body": "Hi {{name}}, your exam {{exam}} is scheduled for {{date}}. Be ready.",
    },
    "exam_submitted": {
        "subject": "Attempt submitted — {{exam}}",
        "body": "Hi {{name}}, your attempt for {{exam}} was submitted. Score: {{score}}.",
    },
    "result_published": {
        "subject": "Result ready — {{exam}}",
        "body": "Hi {{name}}, results for {{exam}} are available. Score: {{score}}.",
    },
    "admin_broadcast": {
        "subject": "{{title}}",
        "body": "{{body}}",
    },
    "password_changed": {
        "subject": "Password changed",
        "body": "Hi {{name}}, your password was changed successfully. If this wasn't you, contact support.",
    },
    "login_alert": {
        "subject": "New login to your account",
        "body": "Hi {{name}}, a new sign-in was detected on your Exam OS account.",
    },
    "maintenance": {
        "subject": "Maintenance notice",
        "body": "{{body}}",
    },
    "subscription_expiry": {
        "subject": "Subscription expiring — {{subscription}}",
        "body": "Hi {{name}}, your plan {{subscription}} expires on {{expiry}}.",
    },
}


def _notifications_globally_on() -> bool:
    return is_enabled("NOTIFICATIONS_ENABLED", True) or is_enabled("ENABLE_NOTIFICATIONS", True)


def _provider_enabled(name: str) -> bool:
    mapping = {
        "in_app": True,
        "email": is_enabled("EMAIL_ENABLED", False) or is_enabled("ENABLE_EMAIL", False),
        "telegram": is_enabled("TELEGRAM_ENABLED", False),
        "whatsapp": is_enabled("WHATSAPP_ENABLED", False),
        "firebase": is_enabled("FIREBASE_ENABLED", False),
        "fcm": is_enabled("FIREBASE_ENABLED", False),
        "discord": is_enabled("DISCORD_ENABLED", False),
        "sms": is_enabled("SMS_ENABLED", False),
        "web_push": is_enabled("WEB_PUSH_ENABLED", False),
    }
    return bool(mapping.get(name, False))


def render_template(template: str, variables: Optional[Mapping[str, Any]] = None) -> str:
    variables = variables or {}

    def repl(match: re.Match) -> str:
        key = match.group(1)
        val = variables.get(key)
        return "" if val is None else str(val)

    return _VAR_RE.sub(repl, template or "")


def get_or_create_preferences(user_id: int) -> NotificationPreference:
    pref = NotificationPreference.query.filter_by(user_id=user_id).first()
    if pref:
        return pref
    pref = NotificationPreference(user_id=user_id)
    db.session.add(pref)
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        pref = NotificationPreference.query.filter_by(user_id=user_id).first()
        if pref:
            return pref
        raise
    return pref


def _user_allows(pref: NotificationPreference, category: str, channel: str) -> bool:
    cat_field = _CATEGORY_PREF.get(category, "system")
    if not bool(getattr(pref, cat_field, True)):
        return False
    ch_map = {
        "in_app": pref.in_app,
        "email": pref.email,
        "telegram": pref.telegram,
        "push": pref.push,
        "web_push": pref.push,
        "whatsapp": pref.whatsapp,
        "sms": pref.sms,
        "firebase": pref.push,
        "fcm": pref.push,
        "discord": True,  # admin/system only usually
    }
    return bool(ch_map.get(channel, False))


def _resolve_template(code: str, channel: str) -> tuple[str, str]:
    tpl = NotificationTemplate.query.filter_by(code=code, channel=channel, is_active=True).first()
    if tpl:
        return tpl.subject_template or "", tpl.body_template
    # fallback channel-agnostic
    tpl = NotificationTemplate.query.filter_by(code=code, is_active=True).first()
    if tpl:
        return tpl.subject_template or "", tpl.body_template
    base = DEFAULT_TEMPLATES.get(code) or DEFAULT_TEMPLATES.get("admin_broadcast") or {
        "subject": "{{title}}",
        "body": "{{body}}",
    }
    return base.get("subject", ""), base.get("body", "{{body}}")


def notify(
    *,
    user_id: Optional[int] = None,
    category: str = "system",
    title: str = "",
    body: str = "",
    template_code: Optional[str] = None,
    variables: Optional[Dict[str, Any]] = None,
    channels: Optional[Sequence[str]] = None,
    priority: str = "normal",
    scheduled_at=None,
    data: Optional[Dict[str, Any]] = None,
    created_by: Optional[int] = None,
    force_in_app: bool = True,
) -> Optional[Notification]:
    """
    Enqueue a notification. Does not block on external providers.
    Call process_queue() to deliver (or rely on request-end / cron).
    """
    if not _notifications_globally_on():
        logger.debug("notifications disabled — skip enqueue")
        return None

    variables = dict(variables or {})
    if title:
        variables.setdefault("title", title)
    if body:
        variables.setdefault("body", body)

    # Fill name from user if missing
    user = User.query.get(user_id) if user_id else None
    if user and "name" not in variables:
        variables["name"] = user.full_name or "User"

    chans: List[str] = list(channels) if channels else ["in_app"]
    if force_in_app and "in_app" not in chans:
        chans.insert(0, "in_app")

    # Filter by global provider flags
    chans = [c for c in chans if c == "in_app" or _provider_enabled(c)]
    if not chans:
        chans = ["in_app"]

    # Render from template when code provided
    final_title, final_body = title, body
    if template_code:
        subj_t, body_t = _resolve_template(template_code, chans[0])
        final_title = render_template(subj_t or title or template_code, variables)
        final_body = render_template(body_t or body, variables)
    else:
        final_title = render_template(title or "Notification", variables)
        final_body = render_template(body or "", variables)

    # User preference filter (except forced security)
    if user_id and category not in ("security_alert", "password_changed", "login_alert"):
        pref = get_or_create_preferences(user_id)
        chans = [c for c in chans if _user_allows(pref, category, c)]
        if not chans:
            logger.debug("user %s muted all channels for %s", user_id, category)
            return None

    n = Notification(
        user_id=user_id,
        category=category[:64],
        title=(final_title or "Notification")[:255],
        body=final_body or "",
        data_json=json.dumps(data or {})[:20000],
        priority=priority if priority in ("low", "normal", "high") else "normal",
        status="queued",
        channels_json=json.dumps(chans),
        scheduled_at=scheduled_at,
        created_by=created_by,
    )
    db.session.add(n)
    db.session.flush()

    for ch in chans:
        db.session.add(
            NotificationDelivery(
                notification_id=n.id,
                provider=ch if ch != "push" else "web_push",
                status="pending",
            )
        )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("notify enqueue failed")
        return None

    # Best-effort immediate process for in_app (already "delivered" locally)
    try:
        process_queue(limit=5, notification_id=n.id)
    except Exception:
        logger.debug("inline process_queue skipped", exc_info=True)

    return n


def broadcast(
    *,
    title: str,
    body: str,
    category: str = "admin_broadcast",
    role: Optional[str] = None,
    user_ids: Optional[Sequence[int]] = None,
    channels: Optional[Sequence[str]] = None,
    created_by: Optional[int] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> int:
    """Fan-out to many users. Returns count enqueued."""
    if not _notifications_globally_on():
        return 0
    if not is_enabled("ENABLE_ADMIN_BROADCAST", True):
        logger.info("admin broadcast disabled by flag")
        return 0

    q = User.query.filter_by(is_active=True)
    if user_ids:
        q = q.filter(User.id.in_(list(user_ids)[:5000]))
    elif role:
        q = q.filter_by(role=role)
    else:
        # default students + guests taking exams
        q = q.filter(User.role.in_(("student", "guest")))

    count = 0
    for u in q.limit(5000).all():
        n = notify(
            user_id=u.id,
            category=category,
            title=title,
            body=body,
            template_code="admin_broadcast",
            variables={**(variables or {}), "name": u.full_name, "title": title, "body": body},
            channels=channels or ["in_app", "email"],
            created_by=created_by,
            priority="high",
        )
        if n:
            count += 1
    return count


# ----- Providers -----


def _deliver_in_app(n: Notification, d: NotificationDelivery) -> None:
    d.status = "sent"
    d.attempts = int(d.attempts or 0) + 1
    if not n.sent_at:
        n.sent_at = utcnow()
    if n.status == "queued":
        n.status = "sent"


def _deliver_email(n: Notification, d: NotificationDelivery) -> None:
    d.attempts = int(d.attempts or 0) + 1
    if not _provider_enabled("email"):
        d.status = "skipped"
        d.last_error = "email provider disabled"
        return
    user = User.query.get(n.user_id) if n.user_id else None
    to_addr = (user.email if user else None) or ""
    if not to_addr or to_addr.endswith(".local"):
        d.status = "skipped"
        d.last_error = "no deliverable email"
        return

    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        d.status = "failed"
        d.last_error = "SMTP_HOST not configured"
        n.status = "partial"
        return

    port = int(os.getenv("SMTP_PORT") or 587)
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_addr = os.getenv("SMTP_FROM") or username or "noreply@examos.local"
    use_tls = (os.getenv("SMTP_TLS") or "true").lower() in ("1", "true", "yes")

    msg = EmailMessage()
    msg["Subject"] = n.title
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(n.body)

    try:
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls(context=context)
                if username:
                    server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                if username:
                    server.login(username, password)
                server.send_message(msg)
        d.status = "sent"
        d.provider_ref = to_addr
        if not n.sent_at:
            n.sent_at = utcnow()
        if n.status in ("queued", "processing"):
            n.status = "sent"
    except Exception as exc:
        d.status = "failed"
        d.last_error = str(exc)[:500]
        n.status = "partial"
        n.error_message = d.last_error
        logger.warning("email deliver failed: %s", type(exc).__name__)


def _deliver_telegram(n: Notification, d: NotificationDelivery) -> None:
    d.attempts = int(d.attempts or 0) + 1
    if not _provider_enabled("telegram"):
        d.status = "skipped"
        d.last_error = "telegram disabled"
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        d.status = "failed"
        d.last_error = "TELEGRAM_BOT_TOKEN missing"
        return

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if n.user_id:
        pref = NotificationPreference.query.filter_by(user_id=n.user_id).first()
        if pref and pref.telegram_chat_id:
            chat_id = pref.telegram_chat_id
    if not chat_id:
        d.status = "skipped"
        d.last_error = "no telegram chat id"
        return

    text = f"*{n.title}*\n{n.body}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
    try:
        req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=10) as resp:  # nosec B310
            body = resp.read().decode("utf-8")
        d.status = "sent"
        d.provider_ref = chat_id
        if not n.sent_at:
            n.sent_at = utcnow()
        if n.status in ("queued", "processing"):
            n.status = "sent"
        logger.debug("telegram ok %s", body[:80])
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        d.status = "failed"
        d.last_error = str(exc)[:500]
        n.status = "partial"
        n.error_message = d.last_error


def _deliver_discord(n: Notification, d: NotificationDelivery) -> None:
    d.attempts = int(d.attempts or 0) + 1
    if not _provider_enabled("discord"):
        d.status = "skipped"
        return
    webhook = os.getenv("DISCORD_WEBHOOK", "").strip()
    if not webhook.startswith("https://"):
        d.status = "failed"
        d.last_error = "DISCORD_WEBHOOK missing"
        return
    payload = json.dumps({"content": f"**{n.title}**\n{n.body}"[:1900]}).encode("utf-8")
    try:
        req = Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=10) as resp:  # nosec B310
            resp.read()
        d.status = "sent"
        if n.status in ("queued", "processing"):
            n.status = "sent"
        if not n.sent_at:
            n.sent_at = utcnow()
    except Exception as exc:
        d.status = "failed"
        d.last_error = str(exc)[:500]
        n.status = "partial"


def _deliver_stub(n: Notification, d: NotificationDelivery, name: str) -> None:
    """WhatsApp / FCM / SMS — configured later without changing callers."""
    d.attempts = int(d.attempts or 0) + 1
    if not _provider_enabled(name):
        d.status = "skipped"
        d.last_error = f"{name} disabled"
        return
    # Provider credentials present?
    key_map = {
        "whatsapp": "WHATSAPP_PROVIDER",
        "firebase": "FIREBASE_PROJECT",
        "fcm": "FIREBASE_PROJECT",
        "sms": "SMS_API_KEY",
        "web_push": "VAPID_PUBLIC_KEY",
    }
    env_key = key_map.get(name, "")
    if env_key and not os.getenv(env_key):
        d.status = "failed"
        d.last_error = f"{env_key} not configured"
        n.status = "partial"
        return
    # Hook point — mark queued for worker
    d.status = "queued_external"
    d.last_error = f"{name} adapter pending worker"


_DELIVERERS = {
    "in_app": _deliver_in_app,
    "email": _deliver_email,
    "telegram": _deliver_telegram,
    "discord": _deliver_discord,
    "whatsapp": lambda n, d: _deliver_stub(n, d, "whatsapp"),
    "firebase": lambda n, d: _deliver_stub(n, d, "firebase"),
    "fcm": lambda n, d: _deliver_stub(n, d, "fcm"),
    "sms": lambda n, d: _deliver_stub(n, d, "sms"),
    "web_push": lambda n, d: _deliver_stub(n, d, "web_push"),
    "push": lambda n, d: _deliver_stub(n, d, "web_push"),
}


def process_queue(limit: int = 50, notification_id: Optional[int] = None) -> int:
    """Process pending deliveries. Returns number processed."""
    if not _notifications_globally_on():
        return 0

    q = NotificationDelivery.query.filter(
        NotificationDelivery.status.in_(("pending", "failed"))
    )
    if notification_id:
        q = q.filter_by(notification_id=notification_id)
    # retry failed only if attempts < 5
    rows = (
        q.order_by(NotificationDelivery.id.asc())
        .limit(limit)
        .all()
    )
    processed = 0
    for d in rows:
        if d.status == "failed" and int(d.attempts or 0) >= 5:
            continue
        n = Notification.query.get(d.notification_id)
        if not n:
            d.status = "cancelled"
            continue
        if n.scheduled_at and n.scheduled_at > utcnow():
            continue
        if n.status == "cancelled":
            d.status = "cancelled"
            continue
        n.status = "processing"
        fn = _DELIVERERS.get(d.provider)
        try:
            if fn:
                fn(n, d)
            else:
                d.status = "skipped"
                d.last_error = f"unknown provider {d.provider}"
        except Exception as exc:
            d.status = "failed"
            d.last_error = str(exc)[:500]
            n.status = "partial"
            logger.exception("delivery error provider=%s", d.provider)
        processed += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("process_queue commit failed")
        return 0
    return processed


def ensure_default_templates() -> None:
    for code, parts in DEFAULT_TEMPLATES.items():
        existing = NotificationTemplate.query.filter_by(code=code, channel="in_app").first()
        if existing:
            continue
        db.session.add(
            NotificationTemplate(
                code=code,
                channel="in_app",
                subject_template=parts.get("subject"),
                body_template=parts.get("body") or "{{body}}",
                is_active=True,
            )
        )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def provider_status() -> Dict[str, Any]:
    return {
        "enabled": _notifications_globally_on(),
        "providers": {
            "in_app": True,
            "email": _provider_enabled("email") and bool(os.getenv("SMTP_HOST")),
            "telegram": _provider_enabled("telegram") and bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            "discord": _provider_enabled("discord") and bool(os.getenv("DISCORD_WEBHOOK")),
            "whatsapp": _provider_enabled("whatsapp") and bool(os.getenv("WHATSAPP_PROVIDER")),
            "firebase": _provider_enabled("firebase") and bool(os.getenv("FIREBASE_PROJECT")),
            "sms": _provider_enabled("sms") and bool(os.getenv("SMS_API_KEY")),
            "web_push": _provider_enabled("web_push") and bool(os.getenv("VAPID_PUBLIC_KEY")),
        },
        "flags": {
            "NOTIFICATIONS_ENABLED": _notifications_globally_on(),
            "ENABLE_ADMIN_BROADCAST": is_enabled("ENABLE_ADMIN_BROADCAST", True),
            "ENABLE_SYSTEM_ALERTS": is_enabled("ENABLE_SYSTEM_ALERTS", True),
            "ENABLE_REMINDERS": is_enabled("ENABLE_REMINDERS", True),
        },
    }
