"""User model — authentication identity for admin and student roles.

Security notes:
    * ``password_hash`` is never included in ``to_dict`` (anti mass-assignment leak).
    * Passwords are hashed with Werkzeug PBKDF2-SHA256 (salted, iterated).
    * ``check_password`` is constant-time at the hash layer (Werkzeug).
    * Role values are constrained to the known CBT roles at the model edge.

Table: ``users`` — column names are a stable API for migrations and routes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Final, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db

# Stable role vocabulary used by JWT claims and admin UI — do not rename values
USER_ROLES: Final = ("admin", "student", "guest")
_DEFAULT_ROLE: Final[str] = "student"
_MAX_PASSWORD_CHARS: Final[int] = 256  # defend against huge-hash CPU DOS
_PASSWORD_HASH_METHOD: Final[str] = "pbkdf2:sha256"


def utcnow() -> datetime:
    """Naive UTC timestamp (matches existing SQLite-friendly column storage)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return None


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(32), nullable=False, default="student")  # admin | student | guest
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    avatar_url = db.Column(db.String(512), nullable=True)
    phone = db.Column(db.String(32), nullable=True, unique=True, index=True)
    # oauth / auth channel metadata (additive)
    google_sub = db.Column(db.String(64), nullable=True, unique=True, index=True)
    auth_provider = db.Column(db.String(32), nullable=True, default="password")  # password|guest|google|phone
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime, nullable=True)

    # --------------------------------------------
    # EXTENSION POINT: Add profile fields (target_exam, city, etc.)
    # --------------------------------------------

    attempts = db.relationship("Attempt", back_populates="user", lazy="dynamic")

    def set_password(self, password: str) -> None:
        """Hash and store a password. Rejects empty / oversized secrets."""
        if password is None:
            raise ValueError("Password is required")
        if not isinstance(password, str):
            raise TypeError("Password must be a string")
        if not password:
            raise ValueError("Password must not be empty")
        if len(password) > _MAX_PASSWORD_CHARS:
            # Avoid spending CPU hashing multi-megabyte attacker payloads
            raise ValueError("Password exceeds maximum allowed length")
        self.password_hash = generate_password_hash(
            password,
            method=_PASSWORD_HASH_METHOD,
        )

    def check_password(self, password: str) -> bool:
        """
        Verify a password against the stored hash.

        Returns False for missing hash, non-string, or empty input without
        raising — callers treat all failures as authentication failure.
        """
        if not password or not isinstance(password, str):
            return False
        if len(password) > _MAX_PASSWORD_CHARS:
            return False
        stored = self.password_hash
        if not stored:
            return False
        try:
            return check_password_hash(stored, password)
        except (ValueError, TypeError, RuntimeError):
            # Corrupt hash in DB must not 500 the login endpoint mid-exam day
            return False

    def is_admin(self) -> bool:
        """Convenience predicate used by services (optional; routes use JWT role)."""
        return (self.role or "") == "admin"

    def to_dict(self, include_email: bool = True) -> Dict[str, Any]:
        """
        Serialize for API responses.

        Never exposes ``password_hash``. Email is optional for leaderboard-style
        payloads that only need display name.
        """
        provider = getattr(self, "auth_provider", None) or "password"
        is_guest = (self.role or "") == "guest" or provider == "guest"
        data: Dict[str, Any] = {
            "id": self.id,
            "full_name": self.full_name,
            "role": self.role,
            "is_active": bool(self.is_active),
            "avatar_url": self.avatar_url,
            "phone": self.phone,
            "auth_provider": provider,
            "is_guest": is_guest,
            "created_at": _iso(self.created_at),
            "last_login_at": _iso(self.last_login_at),
        }
        if include_email:
            # Hide synthetic guest/phone-local identities from clients
            email = self.email or ""
            if is_guest or email.endswith("@guest.local") or email.endswith("@phone.local"):
                data["email"] = None
            else:
                data["email"] = self.email
        return data

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role!r}>"


# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - password_changed_at + force-reset flags (needs auth route support)
# - failed_login_count / lockout columns for brute-force defense at DB layer
# - argon2 hash method once dependency and re-hash-on-login path are added
# --------------------------------------------
