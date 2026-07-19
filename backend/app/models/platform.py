"""Platform monetization models (additive — optional tables).

Plans, subscriptions, payments, coupons, wallet — used when feature flags enable
monetization. Free deployments never need rows here.
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


class Plan(db.Model):
    __tablename__ = "plans"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.Float, default=0.0, nullable=False)
    currency = db.Column(db.String(8), default="INR", nullable=False)
    interval = db.Column(db.String(32), default="month", nullable=False)  # month|year|lifetime|custom
    features_json = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def feature_list(self) -> List[str]:
        try:
            data = json.loads(self.features_json or "[]")
            return [str(x) for x in data] if isinstance(data, list) else []
        except Exception:
            return []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "price": self.price,
            "currency": self.currency,
            "interval": self.interval,
            "features": self.feature_list(),
            "is_active": bool(self.is_active),
            "sort_order": self.sort_order,
        }


class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan_code = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(32), default="active", nullable=False, index=True)
    features_json = db.Column(db.Text, nullable=True)
    starts_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    auto_renew = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def feature_list(self) -> List[str]:
        try:
            data = json.loads(self.features_json or "[]")
            if isinstance(data, list) and data:
                return [str(x) for x in data]
        except Exception:
            pass
        # fallback from plan
        try:
            plan = Plan.query.filter_by(code=self.plan_code).first()
            if plan:
                return plan.feature_list()
        except Exception:
            pass
        return []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "plan_code": self.plan_code,
            "status": self.status,
            "features": self.feature_list(),
            "starts_at": _iso(self.starts_at),
            "expires_at": _iso(self.expires_at),
            "auto_renew": bool(self.auto_renew),
        }


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    provider = db.Column(db.String(32), nullable=False, default="none")
    amount = db.Column(db.Float, nullable=False, default=0.0)
    currency = db.Column(db.String(8), default="INR", nullable=False)
    status = db.Column(db.String(32), default="pending", nullable=False, index=True)
    external_id = db.Column(db.String(128), nullable=True, index=True)
    meta_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        meta = {}
        try:
            meta = json.loads(self.meta_json or "{}")
        except Exception:
            meta = {}
        return {
            "id": self.id,
            "user_id": self.user_id,
            "provider": self.provider,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "external_id": self.external_id,
            "meta": meta if isinstance(meta, dict) else {},
            "created_at": _iso(self.created_at),
        }


class Coupon(db.Model):
    __tablename__ = "coupons"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    percent_off = db.Column(db.Float, default=0.0, nullable=False)
    amount_off = db.Column(db.Float, default=0.0, nullable=False)
    max_redemptions = db.Column(db.Integer, default=0, nullable=False)
    redeemed_count = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "percent_off": self.percent_off,
            "amount_off": self.amount_off,
            "is_active": bool(self.is_active),
            "expires_at": _iso(self.expires_at),
        }


class WalletLedger(db.Model):
    __tablename__ = "wallet_ledger"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    delta = db.Column(db.Float, nullable=False, default=0.0)
    reason = db.Column(db.String(128), nullable=True)
    balance_after = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
