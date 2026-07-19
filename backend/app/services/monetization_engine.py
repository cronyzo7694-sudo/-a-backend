"""Monetization, Ads, Subscription metadata, and Payment provider abstraction.

Business rules live in configuration. Provider SDKs are optional adapters;
without keys the engine stays inert (free platform).

Public::

    get_monetization_snapshot(user) -> dict
    get_ad_config(user, placement) -> dict
    list_plans() -> list
    get_payment_provider() -> PaymentProvider
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.services.config_engine import get_config
from app.services.feature_flags import is_enabled
from app.services.permission_engine import get_user_entitlements

logger = logging.getLogger("exam_os.services.monetization")


# ----- Payment providers (pluggable) ----------------------------------------


class PaymentProvider(ABC):
    name: str = "none"

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def create_order(self, amount: float, currency: str, receipt: str, notes: dict) -> dict:
        ...

    @abstractmethod
    def verify_payment(self, payload: dict) -> bool:
        ...


class NullPaymentProvider(PaymentProvider):
    name = "none"

    def is_configured(self) -> bool:
        return False

    def create_order(self, amount, currency, receipt, notes):
        return {"provider": "none", "status": "disabled", "message": "Payments disabled"}

    def verify_payment(self, payload: dict) -> bool:
        return False


class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def __init__(self):
        self.key_id = os.getenv("PAYMENT_KEY_ID") or os.getenv("RAZORPAY_KEY_ID") or ""
        self.key_secret = os.getenv("PAYMENT_KEY_SECRET") or os.getenv("RAZORPAY_KEY_SECRET") or ""

    def is_configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    def create_order(self, amount, currency, receipt, notes):
        if not self.is_configured():
            return {"provider": self.name, "status": "misconfigured"}
        # Adapter placeholder — real SDK call would go here without coupling business logic
        return {
            "provider": self.name,
            "status": "created",
            "key_id": self.key_id,
            "amount": int(float(amount) * 100),
            "currency": currency,
            "receipt": receipt,
            "notes": notes or {},
            "message": "Create order via Razorpay SDK in deployment adapter",
        }

    def verify_payment(self, payload: dict) -> bool:
        # Signature verification belongs in deployment-specific adapter
        return False


class StripeProvider(PaymentProvider):
    name = "stripe"

    def __init__(self):
        self.secret = os.getenv("PAYMENT_KEY_SECRET") or os.getenv("STRIPE_SECRET_KEY") or ""
        self.publishable = os.getenv("PAYMENT_KEY_ID") or os.getenv("STRIPE_PUBLISHABLE_KEY") or ""

    def is_configured(self) -> bool:
        return bool(self.secret)

    def create_order(self, amount, currency, receipt, notes):
        if not self.is_configured():
            return {"provider": self.name, "status": "misconfigured"}
        return {
            "provider": self.name,
            "status": "created",
            "publishable_key": self.publishable,
            "amount": amount,
            "currency": currency,
            "receipt": receipt,
        }

    def verify_payment(self, payload: dict) -> bool:
        return False


class CashfreeProvider(PaymentProvider):
    name = "cashfree"

    def __init__(self):
        self.key_id = os.getenv("PAYMENT_KEY_ID") or os.getenv("CASHFREE_APP_ID") or ""
        self.key_secret = os.getenv("PAYMENT_KEY_SECRET") or os.getenv("CASHFREE_SECRET") or ""

    def is_configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    def create_order(self, amount, currency, receipt, notes):
        return {
            "provider": self.name,
            "status": "created" if self.is_configured() else "misconfigured",
            "amount": amount,
            "currency": currency,
            "receipt": receipt,
        }

    def verify_payment(self, payload: dict) -> bool:
        return False


class PhonePeProvider(PaymentProvider):
    name = "phonepe"

    def __init__(self):
        self.key_id = os.getenv("PAYMENT_KEY_ID") or os.getenv("PHONEPE_MERCHANT_ID") or ""
        self.key_secret = os.getenv("PAYMENT_KEY_SECRET") or os.getenv("PHONEPE_SALT") or ""

    def is_configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    def create_order(self, amount, currency, receipt, notes):
        return {
            "provider": self.name,
            "status": "created" if self.is_configured() else "misconfigured",
            "amount": amount,
            "currency": currency,
            "receipt": receipt,
        }

    def verify_payment(self, payload: dict) -> bool:
        return False


_PROVIDERS = {
    "none": NullPaymentProvider,
    "razorpay": RazorpayProvider,
    "stripe": StripeProvider,
    "cashfree": CashfreeProvider,
    "phonepe": PhonePeProvider,
}


def get_payment_provider() -> PaymentProvider:
    cfg = get_config()
    if not is_enabled("ENABLE_PAYMENTS") and not cfg.get("payments.enabled"):
        return NullPaymentProvider()
    name = (cfg.get("payments.provider") or os.getenv("PAYMENT_PROVIDER") or "none").lower()
    cls = _PROVIDERS.get(name, NullPaymentProvider)
    return cls()


def list_plans() -> List[Dict[str, Any]]:
    cfg = get_config()
    plans = cfg.get("subscriptions.plans") or []
    if not isinstance(plans, list):
        return []
    # DB overrides
    try:
        from app.models.platform import Plan

        db_plans = Plan.query.filter_by(is_active=True).order_by(Plan.sort_order).all()
        if db_plans:
            return [p.to_dict() for p in db_plans]
    except Exception:
        pass
    return plans


def get_monetization_snapshot(user: Any = None) -> Dict[str, Any]:
    cfg = get_config()
    ent = get_user_entitlements(user)
    return {
        "mode": cfg.get("monetization.mode") or "free",
        "currency": cfg.get("monetization.currency") or "INR",
        "subscriptions_enabled": bool(
            is_enabled("ENABLE_SUBSCRIPTIONS") or cfg.get("subscriptions.enabled")
        ),
        "payments_enabled": bool(is_enabled("ENABLE_PAYMENTS") or cfg.get("payments.enabled")),
        "ads_enabled": bool(is_enabled("ENABLE_ADS") or cfg.get("ads.enabled")),
        "wallet_enabled": is_enabled("ENABLE_WALLET"),
        "coupons_enabled": is_enabled("ENABLE_COUPONS"),
        "referrals_enabled": is_enabled("ENABLE_REFERRALS"),
        "trial_days": cfg.get("monetization.trial_days") or 0,
        "user": {
            "plan_code": ent.get("plan_code"),
            "is_premium": ent.get("is_premium"),
            "status": ent.get("status"),
            "features": sorted(list(ent.get("features") or [])),
        },
        "plans": list_plans() if is_enabled("ENABLE_SUBSCRIPTIONS") or cfg.get("subscriptions.enabled") else [],
        "payment_provider": get_payment_provider().name,
    }


def get_ad_config(user: Any = None, placement: str = "dashboard") -> Dict[str, Any]:
    """Return whether and how to show ads for a placement."""
    cfg = get_config()
    ads = cfg.get("ads") or {}
    enabled = bool(is_enabled("ENABLE_ADS") and ads.get("enabled"))
    result = {
        "enabled": False,
        "provider": ads.get("provider") or "none",
        "client_id": "",
        "placement": placement,
        "slot_enabled": False,
    }
    if not enabled:
        return result

    ent = get_user_entitlements(user)
    if ads.get("hide_for_premium") and ent.get("is_premium"):
        return result

    if user is None and not ads.get("show_for_guests", True):
        return result
    if user is not None and not ent.get("is_premium") and not ads.get("show_for_free_users", True):
        return result

    # Exam / review placements respect disable flags (caller passes placement)
    if placement in ("exam", "exam_player") and ads.get("disable_during_exam", True):
        return result
    if placement in ("review", "result") and ads.get("disable_during_review", True):
        return result

    slots = ads.get("slots") or {}
    slot_key = {
        "website": "website",
        "dashboard": "dashboard",
        "question": "question_page",
        "question_page": "question_page",
        "results": "results_page",
        "results_page": "results_page",
        "result": "results_page",
    }.get(placement, placement)

    if slot_key in slots and not slots.get(slot_key):
        return result

    result["enabled"] = True
    result["client_id"] = ads.get("client_id") or ""
    result["slot_enabled"] = True
    result["provider"] = ads.get("provider") or "none"
    return result
