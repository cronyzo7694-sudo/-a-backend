"""Request validation helpers for Exam OS CBT API.

Public contract (stable — imported by routes)::

    require_fields(data, fields) -> Optional[str]
    is_valid_email(email) -> bool
    is_strong_password(password) -> tuple[bool, str]
    parse_pagination(args, default_page=1, default_per_page=20, max_per_page=100)
        -> tuple[int, int]
    OPTION_KEYS  # list[str] A..J

Design goals:
    * Never raise on bad operator/client input (routes stay simple).
    * Bound sizes to limit ReDoS / memory pressure from huge query strings.
    * Keep demo password policy (min 6) so seeded accounts keep working.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple, Union

from email_validator import EmailNotValidError, validate_email

logger = logging.getLogger("exam_os.utils.validators")

# ---------------------------------------------------------------------------
# Constants (public OPTION_KEYS name must not change)
# ---------------------------------------------------------------------------
OPTION_KEYS: Final[List[str]] = list("ABCDEFGHIJ")

_MIN_PASSWORD_LEN: Final[int] = 6
_MAX_PASSWORD_LEN: Final[int] = 256
_MAX_EMAIL_LEN: Final[int] = 254  # RFC practical upper bound
_MAX_FIELD_NAME_LEN: Final[int] = 64
_MAX_MISSING_LIST: Final[int] = 32

# Default pagination — aligned with Config.DEFAULT_PAGE_SIZE / MAX_PAGE_SIZE
_DEFAULT_PAGE: Final[int] = 1
_DEFAULT_PER_PAGE: Final[int] = 20
_DEFAULT_MAX_PER_PAGE: Final[int] = 100
_ABSOLUTE_MAX_PER_PAGE: Final[int] = 500  # hard ceiling even if caller passes higher

# Reject emails with control characters / obvious injection junk quickly
_EMAIL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Extremely long local/domain parts before calling email-validator (CPU guard)
_EMAIL_BASIC_RE = re.compile(
    r"^[^@\s]{1,64}@[^@\s]{1,255}$",
)


def require_fields(
    data: Optional[Mapping[str, Any]],
    fields: Sequence[str],
) -> Optional[str]:
    """
    Ensure ``data`` contains non-empty values for each name in ``fields``.

    Returns
    -------
    None
        All required fields present and non-empty.
    str
        Human-readable error listing missing fields (safe for JSON ``error``).
    """
    if not isinstance(data, Mapping):
        names = [
            str(f)[:_MAX_FIELD_NAME_LEN]
            for f in (fields or [])[:_MAX_MISSING_LIST]
            if f is not None
        ]
        if not names:
            return "Invalid request body"
        return f"Missing required fields: {', '.join(names)}"

    missing: List[str] = []
    for raw_name in fields or ():
        if raw_name is None:
            continue
        name = str(raw_name)[:_MAX_FIELD_NAME_LEN]
        if not name:
            continue
        value = data.get(name) if name in data else data.get(raw_name)
        if value is None or value == "":
            missing.append(name)
        elif isinstance(value, str) and not value.strip():
            # Whitespace-only counts as missing for form-like fields
            missing.append(name)
        if len(missing) >= _MAX_MISSING_LIST:
            break

    if missing:
        return f"Missing required fields: {', '.join(missing)}"
    return None


def is_valid_email(email: str) -> bool:
    """
    Validate an email address for registration / admin user create.

    Uses ``email-validator`` with deliverability checks disabled (no DNS)
    so offline CBT lab installs still work. Fails closed on any exception.
    """
    if email is None or not isinstance(email, str):
        return False

    candidate = email.strip()
    if not candidate:
        return False
    if len(candidate) > _MAX_EMAIL_LEN:
        return False
    if _EMAIL_CONTROL_RE.search(candidate):
        return False
    # Fast structural gate before heavier library parse
    if not _EMAIL_BASIC_RE.match(candidate):
        return False
    # Disallow consecutive dots / leading dots in a cheap way
    local, _, domain = candidate.partition("@")
    if not local or not domain or ".." in local or ".." in domain:
        return False
    if domain.startswith(".") or domain.endswith("."):
        return False

    # Lab / offline CBT installs use reserved names (e.g. *.local demo accounts).
    # email-validator rejects special-use TLDs; accept a tight structural subset.
    domain_lower = domain.lower()
    if domain_lower.endswith(".local") or domain_lower == "localhost":
        if re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?", domain_lower):
            if re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
                return True
        return False

    try:
        validate_email(candidate, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False
    except Exception:  # noqa: BLE001 — library edge cases must not 500 auth
        logger.debug("is_valid_email unexpected error", exc_info=True)
        return False


def is_strong_password(password: str) -> Tuple[bool, str]:
    """
    Password policy check for register / profile / admin create.

    Current policy (stable for demo accounts ``admin123`` / ``student123``):
        * length >= 6
        * length <= 256 (anti hash-DOS)

    Returns ``(True, "")`` on success or ``(False, reason)`` on failure.
    """
    if password is None or not isinstance(password, str):
        return False, "Password must be at least 6 characters"
    # Do not strip spaces — intentional passwords may include them; only length
    if len(password) < _MIN_PASSWORD_LEN:
        return False, "Password must be at least 6 characters"
    if len(password) > _MAX_PASSWORD_LEN:
        return False, "Password exceeds maximum allowed length"
    # Reject NUL — can break hashing / storage in some stacks
    if "\x00" in password:
        return False, "Password contains invalid characters"
    return True, ""


def parse_pagination(
    args: Any,
    default_page: int = _DEFAULT_PAGE,
    default_per_page: int = _DEFAULT_PER_PAGE,
    max_per_page: int = _DEFAULT_MAX_PER_PAGE,
) -> Tuple[int, int]:
    """
    Parse ``page`` and ``per_page`` from a Flask request args mapping.

    Always returns integers within safe bounds. Never raises.
    """
    # Normalize defaults
    try:
        default_page_i = max(1, int(default_page))
    except (TypeError, ValueError):
        default_page_i = _DEFAULT_PAGE
    try:
        default_per_i = max(1, int(default_per_page))
    except (TypeError, ValueError):
        default_per_i = _DEFAULT_PER_PAGE
    try:
        max_per_i = max(1, min(int(max_per_page), _ABSOLUTE_MAX_PER_PAGE))
    except (TypeError, ValueError):
        max_per_i = _DEFAULT_MAX_PER_PAGE

    def _get(key: str, default: Any) -> Any:
        if args is None:
            return default
        try:
            # Werkzeug MultiDict / dict both support .get
            return args.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    # page
    page = default_page_i
    raw_page = _get("page", default_page_i)
    try:
        # Reject oversized digit strings cheaply
        page_text = str(raw_page).strip()
        if page_text and len(page_text) <= 10 and (
            page_text.isdigit() or (page_text[0] == "-" and page_text[1:].isdigit())
        ):
            page = max(1, int(page_text, 10))
        elif page_text:
            page = default_page_i
    except (TypeError, ValueError):
        page = default_page_i

    # per_page
    per_page = default_per_i
    raw_per = _get("per_page", default_per_i)
    try:
        per_text = str(raw_per).strip()
        if per_text and len(per_text) <= 6 and (
            per_text.isdigit() or (per_text[0] == "-" and per_text[1:].isdigit())
        ):
            per_page = int(per_text, 10)
            per_page = max(1, min(per_page, max_per_i))
        elif per_text:
            per_page = default_per_i
    except (TypeError, ValueError):
        per_page = default_per_i

    return page, per_page


# --------------------------------------------
# EXTENSION POINT: shared sanitize_string / clamp_int helpers for routes
# --------------------------------------------

# --------------------------------------------
# FUTURE IMPROVEMENT (Not implemented to avoid breaking project):
# - Configurable password complexity (upper/digit/symbol) via app config
# - HaveIBeenPwned k-anonymity check on register (needs network + config flag)
# - parse_pagination reading defaults from current_app.config when in context
# --------------------------------------------
