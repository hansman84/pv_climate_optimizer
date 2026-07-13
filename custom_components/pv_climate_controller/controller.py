"""Runtime controller without a direct Home Assistant write dependency."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .command_adapter import ClimateCommandAdapter, Command, CommandResult
from .const import CONF_CLIMATE_ENTITY_ID, CONF_EMS_GRANTED_STAGES_ENTITY_ID, CONF_EMS_STALE_AFTER_S, CONF_ENERGY_POLICY, CONF_SHADOW_MODE, CONF_TEMPERATURE_ENTITY_ID, CONF_ZONE_NAME, ControllerState, EnergyPolicy
from .ems_adapter import parse_grant, requested_stages
from .evaluator import evaluate_zone
from .models import ControllerConfig, EMSGrant, ZoneConfig, ZoneDecision, ZoneInput


@dataclass(slots=True)
class PVClimateController:
    """Coordinates pure decisions and preserves Shadow Mode."""

    config: ControllerConfig
    command_adapter: ClimateCommandAdapter
    last_decision: ZoneDecision | None = None
    last_ems_grant: EMSGrant | None = None
    last_requested_stages: int = 0

    @classmethod
    def from_config(cls, data: Mapping[str, object], options: Mapping[str, object]) -> "PVClimateController":
        """Create runtime only from explicitly configured entities."""
        shadow_mode = bool(options.get(CONF_SHADOW_MODE, data.get(CONF_SHADOW_MODE, True)))
        policy = EnergyPolicy(options.get(CONF_ENERGY_POLICY, data.get(CONF_ENERGY_POLICY, EnergyPolicy.PV_PREFERRED)))
        climate_id = data.get(CONF_CLIMATE_ENTITY_ID)
        temperature_id = data.get(CONF_TEMPERATURE_ENTITY_ID)
        zone = None
        if isinstance(climate_id, str) and isinstance(temperature_id, str):
            zone = ZoneConfig("configured_zone", str(data.get(CONF_ZONE_NAME, "Zone")), climate_id, temperature_id)
        grant_entity = data.get(CONF_EMS_GRANTED_STAGES_ENTITY_ID)
        stale_after = options.get(CONF_EMS_STALE_AFTER_S, data.get(CONF_EMS_STALE_AFTER_S, 300.0))
        config = ControllerConfig(
            shadow_mode=shadow_mode,
            energy_policy=policy,
            zone=zone,
            ems_granted_stages_entity_id=grant_entity if isinstance(grant_entity, str) else None,
            ems_stale_after_s=float(stale_after),
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

    def evaluate_from_states(
        self,
        *,
        temperature_state: object,
        climate_state: str | None,
        ems_grant_state: object = None,
        ems_grant_age_s: float | None = None,
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
        return decision

    async def async_apply_last_decision(self) -> CommandResult:
        """Demonstrate the sole write boundary; Gate C always blocks it."""
        zone_id = self.config.zone.zone_id if self.config.zone else "unconfigured_zone"
        return await self.command_adapter.async_request(Command(zone_id, "Kühlentscheidung"))
