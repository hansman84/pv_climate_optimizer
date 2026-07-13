"""Redacted diagnostics suitable for export."""

from __future__ import annotations

from typing import Any


SENSITIVE_KEYS = {"token", "password", "secret", "authorization", "api_key"}


def redact(value: Any) -> Any:
    """Recursively redact values whose keys can contain credentials."""
    if isinstance(value, dict):
        return {key: "***" if key.casefold() in SENSITIVE_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
