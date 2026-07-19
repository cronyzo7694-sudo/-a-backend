"""Platform configuration, monetization, and feature flag routes.

Additive endpoints under ``/api/platform`` — do not replace existing APIs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.user import User
from app.services.config_engine import get_config, reload_config
from app.services.feature_flags import all_flags, is_enabled
from app.services.monetization_engine import (
    get_ad_config,
    get_monetization_snapshot,
    get_payment_provider,
    list_plans,
)
from app.services.permission_engine import can, get_user_entitlements
from app.utils.decorators import get_current_user, roles_required

platform_bp = Blueprint("platform", __name__)
logger = logging.getLogger("exam_os.routes.platform")


def _optional_user():
    try:
        from flask_jwt_extended import verify_jwt_in_request

        verify_jwt_in_request(optional=True)
        uid = get_jwt_identity()
        if uid is None:
            return None
        return User.query.get(int(uid))
    except Exception:
        return None


@platform_bp.get("/config")
def public_config():
    """Public platform config for frontend boot (no secrets)."""
    cfg = get_config()
    return jsonify(cfg.public_dict())


@platform_bp.get("/features")
def public_features():
    return jsonify({"features": all_flags()})


@platform_bp.get("/monetization")
def monetization_public():
    user = _optional_user()
    return jsonify(get_monetization_snapshot(user))


@platform_bp.get("/ads")
def ads_config():
    user = _optional_user()
    placement = (request.args.get("placement") or "dashboard").strip()[:64]
    return jsonify(get_ad_config(user, placement=placement))


@platform_bp.get("/plans")
def plans():
    if not is_enabled("ENABLE_SUBSCRIPTIONS") and not get_config().get("subscriptions.enabled"):
        return jsonify({"items": [], "enabled": False})
    return jsonify({"items": list_plans(), "enabled": True})


@platform_bp.get("/me/entitlements")
@jwt_required()
def my_entitlements():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    ent = get_user_entitlements(user)
    return jsonify({
        "plan_code": ent.get("plan_code"),
        "is_premium": ent.get("is_premium"),
        "status": ent.get("status"),
        "features": sorted(list(ent.get("features") or [])),
        "permissions": {
            "exam.take": can(user, "exam.take"),
            "analytics.view": can(user, "analytics.view"),
            "ai_coach.view": can(user, "ai_coach.view"),
            "import.questions": can(user, "import.questions"),
            "export.pdf": can(user, "export.pdf"),
            "admin.panel": can(user, "admin.panel"),
        },
    })


@platform_bp.post("/payments/order")
@jwt_required()
def create_payment_order():
    if not is_enabled("ENABLE_PAYMENTS"):
        return jsonify({"error": "Payments disabled"}), 403
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    amount = data.get("amount")
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount_f <= 0 or amount_f > 1_000_000:
        return jsonify({"error": "Invalid amount"}), 400
    currency = str(data.get("currency") or get_config().get("payments.currency") or "INR")[:8]
    provider = get_payment_provider()
    try:
        from app.models.platform import Payment
        import json
        import time

        pay = Payment(
            user_id=user.id,
            provider=provider.name,
            amount=amount_f,
            currency=currency,
            status="pending",
        )
        db.session.add(pay)
        db.session.flush()
        receipt = f"pay-{pay.id}-{int(time.time())}"
        order = provider.create_order(amount_f, currency, receipt, {"user_id": user.id})
        pay.external_id = str(order.get("id") or order.get("order_id") or "")[:128] or None
        pay.meta_json = json.dumps(order)[:10000]
        db.session.commit()
        return jsonify({"payment_id": pay.id, "order": order}), 201
    except Exception:
        db.session.rollback()
        logger.exception("create_payment_order failed")
        order = provider.create_order(
            amount_f, currency, f"u{user.id}", {"user_id": user.id}
        )
        return jsonify({"order": order}), 201


@platform_bp.post("/admin/config/reload")
@roles_required("admin")
def admin_reload_config():
    cfg = reload_config()
    return jsonify({"message": "Configuration reloaded", "config": cfg.public_dict()})


@platform_bp.get("/admin/config")
@roles_required("admin")
def admin_full_config():
    """Admin view of non-secret config tree."""
    raw = get_config().raw()
    # strip any accidental secret-like keys
    payments = raw.get("payments") or {}
    payments.pop("key_secret", None)
    payments.pop("webhook_secret", None)
    raw["payments"] = payments
    return jsonify(raw)
