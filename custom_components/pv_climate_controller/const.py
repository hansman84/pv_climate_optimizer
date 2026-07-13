"""Constants for PV Climate Controller."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "pv_climate_controller"
DEFAULT_NAME = "PV Klimaregler"

CONF_NAME = "name"
CONF_SHADOW_MODE = "shadow_mode"
CONF_ENERGY_POLICY = "energy_policy"
CONF_ZONE_NAME = "zone_name"
CONF_CLIMATE_ENTITY_ID = "climate_entity_id"
CONF_TEMPERATURE_ENTITY_ID = "temperature_entity_id"
CONF_EMS_GRANTED_STAGES_ENTITY_ID = "ems_granted_stages_entity_id"
CONF_EMS_STALE_AFTER_S = "ems_stale_after_s"


class EnergyPolicy(StrEnum):
    """Supported energy policies."""

    STRICT_PV = "strict_pv"
    PV_PREFERRED = "pv_preferred"
    COMFORT_FIRST = "comfort_first"


class ControllerState(StrEnum):
    """Global controller state."""

    DISABLED = "disabled"
    SHADOW = "shadow"
    AUTOMATIC = "automatic"
    DEGRADED = "degraded"


class ZoneState(StrEnum):
    """Per-zone state."""

    IDLE = "idle"
    REQUESTED = "requested"
    QUEUED = "queued"
    RUNNING = "running"
    MANUAL_OVERRIDE = "manual_override"
    UNAVAILABLE = "unavailable"
