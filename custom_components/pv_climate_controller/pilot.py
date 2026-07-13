"""Living-room-only production eligibility.

This is a second guard in addition to Shadow Mode and the disabled command
adapter. It does not issue a command.
"""

from __future__ import annotations

from .models import ControllerConfig


def living_room_pilot_eligible(config: ControllerConfig, granted_stages: int) -> tuple[bool, str]:
    """Allow only an explicitly named living-room pilot with a fresh grant."""
    if config.shadow_mode:
        return False, "shadow_mode"
    if config.zone is None:
        return False, "zone_missing"
    if config.zone.name.strip().casefold() != "wohnzimmer":
        return False, "pilot_living_room_only"
    if granted_stages < 1:
        return False, "ems_grant_missing"
    return True, "pilot_eligible"
