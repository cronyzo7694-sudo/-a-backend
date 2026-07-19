"""Feature Flag Engine — thin facade over Configuration Engine.

Public::

    is_enabled("ENABLE_ADS") -> bool
    is_enabled("ADS") -> bool
    require_feature("ENABLE_IMPORT")  # raises FeatureDisabled
    all_flags() -> dict
"""

from __future__ import annotations

from typing import Dict

from app.services.config_engine import get_config


class FeatureDisabled(Exception):
    def __init__(self, feature: str):
        self.feature = feature
        super().__init__(f"Feature disabled: {feature}")


def _normalize(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    if not n.startswith("ENABLE_"):
        n = f"ENABLE_{n.upper().replace(' ', '_')}"
    else:
        n = n.upper()
    return n


def is_enabled(feature: str, default: bool = False) -> bool:
    key = _normalize(feature)
    if not key:
        return default
    return get_config().feature(key, default=default)


def require_feature(feature: str) -> None:
    if not is_enabled(feature):
        raise FeatureDisabled(_normalize(feature))


def all_flags() -> Dict[str, bool]:
    raw = get_config().get("features") or {}
    return {str(k): bool(v) for k, v in raw.items()}
