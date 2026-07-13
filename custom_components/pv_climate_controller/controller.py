"""Runtime controller without a direct Home Assistant write dependency."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from .command_adapter import ClimateCommandAdapter, Command, CommandResult
from .const import CONF_CLIMATE_ENTITY_ID, CONF_COMFORT_TEMPERATURE, CONF_EMS_GRANTED_STAGES_ENTITY_ID, CONF_EMS_STALE_AFTER_S, CONF_ENERGY_POLICY, CONF_EXPORT_POWER_ENTITY_ID, CONF_EXPORT_POWER_POSITIVE, CONF_HARD_MAX_TEMPERATURE, CONF_MIN_PV_SURPLUS_W, CONF_PV_FORECAST_POWER_ENTITY_ID, CONF_PV_POWER_ENTITY_ID, CONF_SHADOW_MODE, CONF_TEMPERATURE_ENTITY_ID, CONF_ZONE_NAME, ControllerState, EnergyPolicy
from .ems_adapter import parse_grant, requested_stages
from .evaluator import evaluate_zone
from .models import ControllerConfig, EMSGrant, EnergySnapshot, ZoneConfig, ZoneDecision, ZoneInput


def _optional_entity(options: Mapping[str, object], data: Mapping[str, object], key: str) -> str | None:
    """Accept only explicitly selected source entities."""
    value = options.get(key, data.get(key))
    return value if isinstance(value, str) else None


@dataclass(slots=True)
class PVClimateController:
    """Coordinates pure decisions and preserves Shadow Mode."""

    config: ControllerConfig
    command_adapter: ClimateCommandAdapter
    last_decision: ZoneDecision | None = None
    last_ems_grant: EMSGrant | None = None
    last_requested_stages: int = 0
    last_energy: EnergySnapshot = field(default_factory=EnergySnapshot)
    _state_listeners: list[Callable[[], None]] = field(default_factory=list)

    @classmethod
    def from_config(cls, data: Mapping[str, object], options: Mapping[str, object]) -> "PVClimateController":
        """Create runtime only from explicitly configured entities."""
        shadow_mode = bool(options.get(CONF_SHADOW_MODE, data.get(CONF_SHADOW_MODE, True)))
        policy = EnergyPolicy(options.get(CONF_ENERGY_POLICY, data.get(CONF_ENERGY_POLICY, EnergyPolicy.PV_PREFERRED)))
        climate_id = options.get(CONF_CLIMATE_ENTITY_ID, data.get(CONF_CLIMATE_ENTITY_ID))
        temperature_id = options.get(CONF_TEMPERATURE_ENTITY_ID, data.get(CONF_TEMPERATURE_ENTITY_ID))
        zone = None
        if isinstance(climate_id, str) and isinstance(temperature_id, str):
            comfort = float(options.get(CONF_COMFORT_TEMPERATURE, data.get(CONF_COMFORT_TEMPERATURE, 24.0)))
            hard_max = float(options.get(CONF_HARD_MAX_TEMPERATURE, data.get(CONF_HARD_MAX_TEMPERATURE, 25.0)))
            zone = ZoneConfig(
                "configured_zone",
                str(options.get(CONF_ZONE_NAME, data.get(CONF_ZONE_NAME, "Zone"))),
                climate_id,
                temperature_id,
                comfort_temperature=comfort,
                hard_max_temperature=max(comfort, hard_max),
            )
        grant_entity = options.get(CONF_EMS_GRANTED_STAGES_ENTITY_ID, data.get(CONF_EMS_GRANTED_STAGES_ENTITY_ID))
        stale_after = options.get(CONF_EMS_STALE_AFTER_S, data.get(CONF_EMS_STALE_AFTER_S, 300.0))
        config = ControllerConfig(
            shadow_mode=shadow_mode,
            energy_policy=policy,
            zone=zone,
            ems_granted_stages_entity_id=grant_entity if isinstance(grant_entity, str) else None,
            ems_stale_after_s=float(stale_after),
            pv_power_entity_id=_optional_entity(options, data, CONF_PV_POWER_ENTITY_ID),
            export_power_entity_id=_optional_entity(options, data, CONF_EXPORT_POWER_ENTITY_ID),
            export_power_positive=bool(options.get(CONF_EXPORT_POWER_POSITIVE, data.get(CONF_EXPORT_POWER_POSITIVE, True))),
            pv_forecast_power_entity_id=_optional_entity(options, data, CONF_PV_FORECAST_POWER_ENTITY_ID),
            min_pv_surplus_w=float(options.get(CONF_MIN_PV_SURPLUS_W, data.get(CONF_MIN_PV_SURPLUS_W, 1000.0))),
        )
        return cls(config=config, command_adapter=ClimateCommandAdapter(shadow_mode=shadow_mode, productive_enabled=False))

    @property
    def state(self) -> ControllerState:
        """Return an explicit, fail-safe global state."""
        if self.config.shadow_mode:
            return ControllerState.SHADOW
        return ControllerState.DISABLED

    def evaluate(self, sample: ZoneInput) -> ZoneDecision | None:
        """Create a zone decision only; no transport is invoked."""
        if self.config.zone is None:
            self.last_decision = None
            return None
        self.last_decision = evaluate_zone(self.config.zone, sample)
        return self.last_decision

    def evaluate_ems(self, grant_value: object, grant_age_s: float | None) -> EMSGrant:
        """Evaluate capacity only; a missing grant fails safely to zero stages."""
        self.last_requested_stages = requested_stages(bool(self.last_decision and self.last_decision.demand))
        self.last_ems_grant = parse_grant(grant_value, grant_age_s, self.config.ems_stale_after_s)
        return self.last_ems_grant

    @staticmethod
    def _power_w(value: object, unit: object) -> float | None:
        """Normalize a configured power sensor to watts; reject unknown units."""
        try:
            reading = float(str(value))
        except (TypeError, ValueError):
            return None
        normalized_unit = str(unit or "W").strip().lower()
        if normalized_unit == "w":
            return reading
        if normalized_unit == "kw":
            return reading * 1000
        return None

    def evaluate_energy(
        self,
        *,
        pv_power_state: object = None,
        pv_power_unit: object = None,
        export_power_state: object = None,
        export_power_unit: object = None,
        pv_forecast_power_state: object = None,
        pv_forecast_power_unit: object = None,
    ) -> EnergySnapshot:
        """Read configured PV values only; this does not affect a climate device."""
        pv_power = self._power_w(pv_power_state, pv_power_unit) if self.config.pv_power_entity_id else None
        export_power = self._power_w(export_power_state, export_power_unit) if self.config.export_power_entity_id else None
        if export_power is not None and not self.config.export_power_positive:
            export_power *= -1
        forecast = self._power_w(pv_forecast_power_state, pv_forecast_power_unit) if self.config.pv_forecast_power_entity_id else None
        self.last_energy = EnergySnapshot(pv_power, export_power, forecast)
        return self.last_energy

    def evaluate_from_states(
        self,
        *,
        temperature_state: object,
        climate_state: str | None,
        ems_grant_state: object = None,
        ems_grant_age_s: float | None = None,
        pv_power_state: object = None,
        pv_power_unit: object = None,
        export_power_state: object = None,
        export_power_unit: object = None,
        pv_forecast_power_state: object = None,
        pv_forecast_power_unit: object = None,
    ) -> ZoneDecision | None:
        """Evaluate raw HA state values without importing or writing to HA."""
        try:
            temperature = float(str(temperature_state))
        except (TypeError, ValueError):
            temperature = None
        decision = self.evaluate(
            ZoneInput(
                temperature_c=temperature,
                climate_available=climate_state not in {None, "unknown", "unavailable"},
                manual_override=bool(self.config.zone and self.command_adapter.is_manual_override(self.config.zone.climate_entity_id)),
            )
        )
        self.evaluate_ems(ems_grant_state, ems_grant_age_s)
        self.evaluate_energy(
            pv_power_state=pv_power_state,
            pv_power_unit=pv_power_unit,
            export_power_state=export_power_state,
            export_power_unit=export_power_unit,
            pv_forecast_power_state=pv_forecast_power_state,
            pv_forecast_power_unit=pv_forecast_power_unit,
        )
        return decision

    def add_state_listener(self, listener: Callable[[], None]) -> None:
        """Register an entity refresh callback without depending on HA types."""
        self._state_listeners.append(listener)

    def remove_state_listener(self, listener: Callable[[], None]) -> None:
        """Remove a previously registered entity refresh callback."""
        if listener in self._state_listeners:
            self._state_listeners.remove(listener)

    def notify_state_listeners(self) -> None:
        """Refresh diagnostic entities after a Shadow Mode evaluation."""
        for listener in tuple(self._state_listeners):
            listener()

    def set_shadow_mode(self, enabled: bool) -> None:
        """Update the UI-visible mode; the command adapter remains hard locked."""
        self.config = replace(self.config, shadow_mode=enabled)

    def set_energy_policy(self, policy: EnergyPolicy) -> None:
        """Update the selected evaluation policy."""
        self.config = replace(self.config, energy_policy=policy)

    def set_comfort_temperature(self, temperature: float) -> None:
        """Update the zone comfort threshold."""
        if self.config.zone is None:
            return
        hard_max = max(temperature, self.config.zone.hard_max_temperature)
        self.config = replace(self.config, zone=replace(self.config.zone, comfort_temperature=temperature, hard_max_temperature=hard_max))

    def set_hard_max_temperature(self, temperature: float) -> None:
        """Update the zone hard limit without allowing it below comfort."""
        if self.config.zone is None:
            return
        hard_max = max(temperature, self.config.zone.comfort_temperature)
        self.config = replace(self.config, zone=replace(self.config.zone, hard_max_temperature=hard_max))

    def set_min_pv_surplus_w(self, watts: float) -> None:
        """Update the diagnostic PV threshold without enabling control."""
        self.config = replace(self.config, min_pv_surplus_w=max(0.0, watts))

    def set_export_power_positive(self, positive_when_exporting: bool) -> None:
        """Set only the display normalization convention for the selected source."""
        self.config = replace(self.config, export_power_positive=positive_when_exporting)

    async def async_apply_last_decision(self) -> CommandResult:
        """Demonstrate the sole write boundary; Gate C always blocks it."""
        zone_id = self.config.zone.zone_id if self.config.zone else "unconfigured_zone"
        return await self.command_adapter.async_request(Command(zone_id, "Kühlentscheidung"))
