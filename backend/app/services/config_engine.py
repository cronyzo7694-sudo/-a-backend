"""Central Configuration Engine for Exam OS.

Priority (highest wins):
    1. Environment variables
    2. Optional JSON file (EXAM_OS_CONFIG_FILE or backend/exam_os.config.json)
    3. Built-in defaults

Public API (stable)::

    get_config() -> PlatformConfig
    get_setting(path, default=None)
    reload_config()
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional

logger = logging.getLogger("exam_os.services.config_engine")

_LOCK = threading.RLock()
_CACHE: Optional["PlatformConfig"] = None

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG_PATH = _BACKEND_ROOT / "exam_os.config.json"

# ---------------------------------------------------------------------------
# Built-in defaults — every key is overridable
# ---------------------------------------------------------------------------
DEFAULT_PLATFORM_CONFIG: Dict[str, Any] = {
    "app": {
        "name": "परीक्षa",
        "version": "1.0.0",
        "environment": "development",
        "timezone": "Asia/Kolkata",
        "default_theme": "default",
        "default_locale": "en",
        "support_email": "",
        "public_url": "",
    },
    "maintenance": {
        "enabled": False,
        "read_only": False,
        "admin_only": False,
        "emergency_lock": False,
        "message": "We are undergoing scheduled maintenance. Please try again shortly.",
        "allow_paths": ["/api/health", "/api/auth/login", "/api/platform/config"],
    },
    "security": {
        "max_upload_bytes": 16 * 1024 * 1024,
        "rate_limit_enabled": False,
        "login_max_attempts": 20,
        "audit_log_enabled": True,
        "require_https": False,
    },
    "storage": {
        "provider": "local",  # local | s3 | gcs (future)
        "upload_folder": "uploads",
        "public_base_url": "",
    },
    "email": {
        "enabled": False,
        "provider": "smtp",  # smtp | sendgrid | ses (future)
        "from_address": "",
    },
    "localization": {
        "default": "en",
        "allowed": ["en", "hi"],
    },
    "features": {
        "ENABLE_ADS": False,
        "ENABLE_SUBSCRIPTIONS": False,
        "ENABLE_AI_COACH": True,
        "ENABLE_ANALYTICS": True,
        "ENABLE_BOOKMARKS": True,
        "ENABLE_NOTES": False,
        "ENABLE_WRONG_NOTEBOOK": True,
        "ENABLE_PDF_EXPORT": False,
        "ENABLE_IMPORT": True,
        "ENABLE_EXPORT": False,
        "ENABLE_LEADERBOARD": False,
        "ENABLE_REFERRALS": False,
        "ENABLE_COUPONS": False,
        "ENABLE_WALLET": False,
        "ENABLE_CHAT": False,
        "ENABLE_DARK_THEME": True,
        "ENABLE_OFFLINE_MODE": False,
        "ENABLE_ADMIN_PANEL": True,
        "ENABLE_NOTIFICATIONS": False,
        "ENABLE_EMAIL": False,
        "ENABLE_MAINTENANCE_MODE": False,
        "ENABLE_PAYMENTS": False,
        "ENABLE_TRIAL": True,
        "ENABLE_INSTITUTION_LICENSE": False,
        "ENABLE_QUESTION_BANK": True,
        "ENABLE_EXAM_RULES": True,
        "ENABLE_GUEST_ACCESS": True,
        "ENABLE_GOOGLE_LOGIN": False,
        "ENABLE_PHONE_OTP": False,
        "ENABLE_EMAIL_PASSWORD_LOGIN": True,
        # Notification engine flags
        "NOTIFICATIONS_ENABLED": True,
        "ENABLE_NOTIFICATIONS": True,
        "TELEGRAM_ENABLED": False,
        "EMAIL_ENABLED": False,
        "WHATSAPP_ENABLED": False,
        "FIREBASE_ENABLED": False,
        "DISCORD_ENABLED": False,
        "SMS_ENABLED": False,
        "WEB_PUSH_ENABLED": False,
        "ENABLE_ADMIN_BROADCAST": True,
        "ENABLE_SYSTEM_ALERTS": True,
        "ENABLE_REMINDERS": True,
        "ENABLE_AI_NOTIFICATIONS": False,
    },
    "auth": {
        "google_client_id": "",  # also GOOGLE_CLIENT_ID env
        "phone_otp_length": 6,
        "phone_otp_ttl_seconds": 300,
        "phone_otp_max_attempts": 5,
        # When no SMS provider configured, OTP is accepted in dev via response (never production)
        "phone_otp_dev_expose": False,
    },
    "notifications": {
        "default_channels": ["in_app"],
        "max_broadcast_recipients": 5000,
        "queue_batch_size": 50,
    },
    "monetization": {
        "mode": "free",  # free | ads | subscription | hybrid | institution | lifetime
        "currency": "INR",
        "trial_days": 7,
        "grace_period_days": 3,
        "free_daily_exam_limit": 0,  # 0 = unlimited
        "free_daily_question_limit": 0,
        "device_limit": 0,
        "concurrent_login_limit": 0,
    },
    "ads": {
        "enabled": False,
        "provider": "none",  # none | adsense | gam | custom
        "client_id": "",
        "slots": {
            "website": False,
            "dashboard": False,
            "question_page": False,
            "results_page": False,
        },
        "show_for_free_users": True,
        "show_for_guests": True,
        "hide_for_premium": True,
        "disable_during_exam": True,
        "disable_during_review": True,
    },
    "subscriptions": {
        "enabled": False,
        "plans": [
            {
                "code": "free",
                "name": "Free",
                "price": 0,
                "interval": "lifetime",
                "features": ["exams.practice", "analytics.basic"],
            },
            {
                "code": "monthly",
                "name": "Monthly",
                "price": 199,
                "interval": "month",
                "features": ["exams.*", "analytics.*", "ai_coach", "import"],
            },
            {
                "code": "yearly",
                "name": "Yearly",
                "price": 1499,
                "interval": "year",
                "features": ["exams.*", "analytics.*", "ai_coach", "import", "export"],
            },
            {
                "code": "lifetime",
                "name": "Lifetime",
                "price": 4999,
                "interval": "lifetime",
                "features": ["*"],
            },
        ],
        "auto_renew_default": False,
    },
    "payments": {
        "enabled": False,
        "provider": "none",  # none | razorpay | stripe | cashfree | phonepe
        "currency": "INR",
        "success_url": "",
        "cancel_url": "",
        # Keys NEVER stored here — only env: PAYMENT_KEY_ID, PAYMENT_KEY_SECRET
    },
    "exam_defaults": {
        # Soft defaults; ExamRuleEngine remains source for per-exam rules
        "default_mode": "mock",
        "default_duration_seconds": 3600,
    },
}


def _deep_merge(base: MutableMapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_file_config() -> Dict[str, Any]:
    path = os.getenv("EXAM_OS_CONFIG_FILE") or str(_DEFAULT_CONFIG_PATH)
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Failed to load config file %s", path)
        return {}


def _env_bool(name: str) -> Optional[bool]:
    raw = os.getenv(name)
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return None


def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Map well-known env vars onto config tree."""
    c = copy.deepcopy(cfg)

    env = os.getenv("FLASK_ENV") or os.getenv("EXAM_OS_ENV")
    if env:
        c.setdefault("app", {})["environment"] = env.strip().lower()

    name = os.getenv("EXAM_OS_APP_NAME")
    if name:
        c.setdefault("app", {})["name"] = name.strip()

    tz = os.getenv("EXAM_OS_TIMEZONE")
    if tz:
        c.setdefault("app", {})["timezone"] = tz.strip()

    # Maintenance
    m = _env_bool("ENABLE_MAINTENANCE_MODE")
    if m is not None:
        c.setdefault("features", {})["ENABLE_MAINTENANCE_MODE"] = m
        c.setdefault("maintenance", {})["enabled"] = m
    msg = os.getenv("MAINTENANCE_MESSAGE")
    if msg:
        c.setdefault("maintenance", {})["message"] = msg
    if _env_bool("MAINTENANCE_ADMIN_ONLY") is True:
        c.setdefault("maintenance", {})["admin_only"] = True
    if _env_bool("MAINTENANCE_READ_ONLY") is True:
        c.setdefault("maintenance", {})["read_only"] = True

    # Feature flags ENABLE_* from env
    features = c.setdefault("features", {})
    for key in list(DEFAULT_PLATFORM_CONFIG["features"].keys()):
        val = _env_bool(key)
        if val is not None:
            features[key] = val
    # Alias
    if _env_bool("ENABLE_GUEST") is not None:
        features["ENABLE_GUEST_ACCESS"] = bool(_env_bool("ENABLE_GUEST"))

    # Auth providers
    google_cid = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("EXAM_OS_GOOGLE_CLIENT_ID")
    if google_cid:
        c.setdefault("auth", {})["google_client_id"] = google_cid.strip()
        # Convenience: if client id set and flag unset, leave flag to explicit ENABLE_GOOGLE_LOGIN
    if _env_bool("PHONE_OTP_DEV_EXPOSE") is not None:
        c.setdefault("auth", {})["phone_otp_dev_expose"] = bool(_env_bool("PHONE_OTP_DEV_EXPOSE"))

    # Monetization mode
    mode = os.getenv("MONETIZATION_MODE")
    if mode:
        c.setdefault("monetization", {})["mode"] = mode.strip().lower()

    # Ads
    if _env_bool("ENABLE_ADS") is not None:
        c.setdefault("ads", {})["enabled"] = bool(_env_bool("ENABLE_ADS"))
        c.setdefault("features", {})["ENABLE_ADS"] = bool(_env_bool("ENABLE_ADS"))
    ad_provider = os.getenv("AD_PROVIDER")
    if ad_provider:
        c.setdefault("ads", {})["provider"] = ad_provider.strip().lower()
    ad_client = os.getenv("AD_CLIENT_ID")
    if ad_client:
        c.setdefault("ads", {})["client_id"] = ad_client.strip()

    # Subscriptions / payments
    if _env_bool("ENABLE_SUBSCRIPTIONS") is not None:
        on = bool(_env_bool("ENABLE_SUBSCRIPTIONS"))
        c.setdefault("features", {})["ENABLE_SUBSCRIPTIONS"] = on
        c.setdefault("subscriptions", {})["enabled"] = on
    if _env_bool("ENABLE_PAYMENTS") is not None:
        on = bool(_env_bool("ENABLE_PAYMENTS"))
        c.setdefault("features", {})["ENABLE_PAYMENTS"] = on
        c.setdefault("payments", {})["enabled"] = on
    pay_provider = os.getenv("PAYMENT_PROVIDER")
    if pay_provider:
        c.setdefault("payments", {})["provider"] = pay_provider.strip().lower()

    # Sync legacy Flask Config feature keys if present in env
    for legacy, flag in (
        ("FEATURE_NEGATIVE_MARKING", None),
        ("FEATURE_SECTION_LOCK", None),
    ):
        pass  # exam rules handle these; kept for Flask Config compatibility elsewhere

    return c


def build_platform_config() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_PLATFORM_CONFIG)
    file_cfg = _load_file_config()
    if file_cfg:
        cfg = _deep_merge(cfg, file_cfg)
    cfg = _apply_env_overrides(cfg)
    # Derive maintenance.enabled from feature flag if set
    if cfg.get("features", {}).get("ENABLE_MAINTENANCE_MODE"):
        cfg.setdefault("maintenance", {})["enabled"] = True
    return cfg


class PlatformConfig:
    """Immutable-ish snapshot of merged platform configuration."""

    def __init__(self, data: Mapping[str, Any]):
        self._data = copy.deepcopy(dict(data))

    def raw(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in path.split("."):
            if not isinstance(cur, Mapping) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def feature(self, name: str, default: bool = False) -> bool:
        features = self._data.get("features") or {}
        if name in features:
            return bool(features[name])
        # allow bare name without ENABLE_
        key = name if name.startswith("ENABLE_") else f"ENABLE_{name}"
        return bool(features.get(key, default))

    def public_dict(self) -> Dict[str, Any]:
        """Client-safe subset (no secrets, no payment keys)."""
        ads = copy.deepcopy(self.get("ads") or {})
        # never expose server secrets
        payments = {
            "enabled": bool(self.get("payments.enabled")),
            "provider": self.get("payments.provider") if self.get("payments.enabled") else "none",
            "currency": self.get("payments.currency") or "INR",
        }
        return {
            "app": self.get("app") or {},
            "features": self.get("features") or {},
            "monetization": {
                "mode": self.get("monetization.mode") or "free",
                "currency": self.get("monetization.currency") or "INR",
                "trial_days": self.get("monetization.trial_days") or 0,
            },
            "ads": {
                "enabled": bool(ads.get("enabled")),
                "provider": ads.get("provider") or "none",
                "client_id": ads.get("client_id") or "",
                "slots": ads.get("slots") or {},
                "show_for_free_users": bool(ads.get("show_for_free_users", True)),
                "show_for_guests": bool(ads.get("show_for_guests", True)),
                "hide_for_premium": bool(ads.get("hide_for_premium", True)),
                "disable_during_exam": bool(ads.get("disable_during_exam", True)),
                "disable_during_review": bool(ads.get("disable_during_review", True)),
            },
            "subscriptions": {
                "enabled": bool(self.get("subscriptions.enabled")),
                "plans": self.get("subscriptions.plans") or [],
            },
            "payments": payments,
            "localization": self.get("localization") or {},
            "maintenance": {
                "enabled": bool(self.get("maintenance.enabled")),
                "message": self.get("maintenance.message") or "",
            },
            "theme": self.get("app.default_theme") or "default",
            "auth": {
                "google_client_id": self.get("auth.google_client_id") or "",
                "guest": self.feature("ENABLE_GUEST_ACCESS", True),
                "google": self.feature("ENABLE_GOOGLE_LOGIN", False),
                "phone_otp": self.feature("ENABLE_PHONE_OTP", False),
                "email_password": self.feature("ENABLE_EMAIL_PASSWORD_LOGIN", True),
            },
        }


def get_config(force_reload: bool = False) -> PlatformConfig:
    global _CACHE
    with _LOCK:
        if _CACHE is None or force_reload:
            _CACHE = PlatformConfig(build_platform_config())
        return _CACHE


def reload_config() -> PlatformConfig:
    return get_config(force_reload=True)


def get_setting(path: str, default: Any = None) -> Any:
    return get_config().get(path, default)
