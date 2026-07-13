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
    comfort_temperature: float = 23.5
    hard_max_temperature: float = 25.5
    cooling_power_entity_id: str | None = None
    priority: int = 50
    minimum_plausible_temperature_c: float = 5.0
    maximum_plausible_temperature_c: float = 50.0


@dataclass(frozen=True, slots=True)
class ZoneInput:
    """Validated input required for one evaluation."""

    temperature_c: float | None
    climate_available: bool
    manual_override: bool = False


@dataclass(frozen=True, slots=True)
class ZoneForecast:
    """A conservative, read-only temperature outlook for a room."""

    zone_id: str
    trend_c_per_h: float | None
    predicted_temperature_60m_c: float | None
    sample_count: int
    data_quality: str


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
    pv_power_entity_id: str | None = None
    export_power_entity_id: str | None = None
    export_power_positive: bool = True
    pv_forecast_power_entity_id: str | None = None
    min_pv_surplus_w: float = 1000.0
    house_zones: tuple[ZoneConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class EMSGrant:
    """Validated capacity response from any external EMS."""

    stages: int
    available: bool
    reason_code: str
    reason_text: str


@dataclass(frozen=True, slots=True)
class EnergySnapshot:
    """Normalized, read-only energy values used for diagnostics."""

    pv_power_w: float | None = None
    export_power_w: float | None = None
    pv_forecast_power_w: float | None = None
