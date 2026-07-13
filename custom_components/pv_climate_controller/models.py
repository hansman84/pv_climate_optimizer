"""Pure data models for controller decisions."""

from __future__ import annotations

from dataclasses import dataclass

from .const import EnergyPolicy, ZoneState


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    """A user-selected, never inferred zone mapping."""

    zone_id: str
    name: str
    climate_entity_id: str
    temperature_entity_id: str
    comfort_temperature: float = 24.0
    hard_max_temperature: float = 25.0


@dataclass(frozen=True, slots=True)
class ZoneInput:
    """Validated input required for one evaluation."""

    temperature_c: float | None
    climate_available: bool
    manual_override: bool = False


@dataclass(frozen=True, slots=True)
class ZoneDecision:
    """Recorder-friendly result of one zone evaluation."""

    zone_id: str
    state: ZoneState
    demand: bool
    score: float
    requested: bool
    reason_code: str
    reason_text: str


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    """Global settings required in Gate C."""

    shadow_mode: bool
    energy_policy: EnergyPolicy
    zone: ZoneConfig | None = None
    ems_granted_stages_entity_id: str | None = None
    ems_stale_after_s: float = 300.0


@dataclass(frozen=True, slots=True)
class EMSGrant:
    """Validated capacity response from any external EMS."""

    stages: int
    available: bool
    reason_code: str
    reason_text: str
