"""Schema-versioned, secret-free controller snapshot helpers."""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1


def pack(runtime_state: dict[str, Any]) -> dict[str, Any]:
    """Wrap a runtime snapshot for later HA Store persistence."""
    return {"version": SCHEMA_VERSION, "runtime": runtime_state}


def unpack(snapshot: object) -> dict[str, Any]:
    """Fail closed to an empty snapshot for unknown storage shapes."""
    if not isinstance(snapshot, dict) or snapshot.get("version") != SCHEMA_VERSION:
        return {}
    runtime = snapshot.get("runtime")
    return dict(runtime) if isinstance(runtime, dict) else {}
