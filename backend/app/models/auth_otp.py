"""Phone OTP challenges — used only when ENABLE_PHONE_OTP is on."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PhoneOtp(db.Model):
    __tablename__ = "phone_otps"

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(32), nullable=False, index=True)
    code_hash = db.Column(db.String(255), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at < utcnow())

    def is_consumed(self) -> bool:
        return self.consumed_at is not None
