"""Compatibility home for future rate-limit configuration.

Rate enforcement belongs to ClimateCommandAdapter in Gate D so no alternative
write path can bypass it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    global_interval_s: float = 30.0
    per_entity_interval_s: float = 300.0
