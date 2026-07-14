"""Pure helpers for safely updating config-entry options."""

from __future__ import annotations

from typing import Any


def merge_safety_options(
    current: dict[str, Any], submitted: dict[str, Any], ems_key: str, enabled_key: str
) -> dict[str, Any]:
    """Persist the dedicated EMS toggle as an explicit source removal."""
    merged = {**current, **submitted}
    enabled = bool(merged.pop(enabled_key, current.get(ems_key)))
    if not enabled or not submitted.get(ems_key):
        # Config-entry data is still part of the effective configuration.
        # ``None`` deliberately overrides a formerly selected source.
        merged[ems_key] = None
    return merged
