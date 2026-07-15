"""Runtime controller without a direct Home Assistant write dependency."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from time import monotonic

from .command_adapter import ClimateCommandAdapter, Command, CommandResult
from .const import CONF_CLIMATE_ENTITY_ID, CONF_COMFORT_TEMPERATURE, CONF_EMS_GRANTED_STAGES_ENTITY_ID, CONF_EMS_STALE_AFTER_S, CONF_ENERGY_POLICY, CONF_EXPORT_POWER_ENTITY_ID, CONF_EXPORT_POWER_POSITIVE, CONF_HARD_MAX_TEMPERATURE, CONF_HOUSE_ZONES, CONF_LIVING_ROOM_PILOT_ENABLED, CONF_MIN_PV_SURPLUS_W, CONF_OUTDOOR_TEMPERATURE_ENTITY_ID, CONF_OUTDOOR_UNIT_POWER_ENTITY_ID, CONF_PV_FORECAST_POWER_ENTITY_ID, CONF_PV_POWER_ENTITY_ID, CONF_SHADOW_MODE, CONF_SOLAR_IRRADIANCE_ENTITY_ID, CONF_SUN_ENTITY_ID, CONF_TEMPERATURE_ENTITY_ID, CONF_ZONE_NAME, ControllerState, EnergyPolicy
from .ems_adapter import parse_grant, requested_stages
from .evaluator import evaluate_zone
from .forecasting import predicted_temperature_60m, temperature_trend_c_per_h
from .house import HousePlan, ZoneTelemetry, build_house_plan
from .house_learning import HouseLearningModel
from .models import ControllerConfig, EMSGrant, EnergySnapshot, ThermalProfile, ThermalResponse, ZoneConfig, ZoneDecision, ZoneForecast, ZoneInput
from .outdoor_unit import HISENSE_5AMW125U4RTA
from .pilot import LivingRoomPilot, PilotAction
from .power_learning import OutdoorPowerLearner, PowerEstimate
from .thermal_budget import build_thermal_budget
from .thermal_response import learn_thermal_response
from .thermal_analysis import learn_thermal_profile


def _optional_entity(options: Mapping[str, object], data: Mapping[str, object], key: str) -> str | None:
    """Accept only explicitly selected source entities."""
    value = options.get(key, data.get(key))
    return value if isinstance(value, str) else None


def _house_zones(value: object) -> tuple[ZoneConfig, ...]:
    """Load only complete, explicitly configured zone records."""
    if not isinstance(value, list):
        return ()
    result: list[ZoneConfig] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name, climate, temperature = item.get("name"), item.get("climate_entity_id"), item.get("temperature_entity_id")
        if not all(isinstance(field, str) for field in (name, climate, temperature)):
            continue
        shade_ids = tuple(entity for entity in item.get("shade_entity_ids", []) if isinstance(entity, str)) if isinstance(item.get("shade_entity_ids"), list) else ()
        azimuths = tuple(float(entry) for entry in item.get("facade_azimuths", []) if isinstance(entry, (int, float))) if isinstance(item.get("facade_azimuths"), list) else ()
        raw_facade_shades = item.get("facade_shade_entity_ids", [])
        facade_shades = tuple(tuple(entity for entity in group if isinstance(entity, str)) for group in raw_facade_shades if isinstance(group, list)) if isinstance(raw_facade_shades, list) else ()
        cutoff = item.get("overhang_cutoff_elevation")
        result.append(ZoneConfig(
            zone_id=str(item.get("zone_id", climate)), name=name, climate_entity_id=climate,
            temperature_entity_id=temperature, comfort_temperature=float(item.get("comfort_temperature", 23.5)),
            hard_max_temperature=float(item.get("hard_max_temperature", 25.5)),
            cooling_power_entity_id=item.get("cooling_power_entity_id") if isinstance(item.get("cooling_power_entity_id"), str) else None,
            priority=int(item.get("priority", 50)),
            use_climate_temperature_fallback=bool(item.get("use_climate_temperature_fallback", False)),
            shade_entity_ids=shade_ids,
            facade_azimuths=azimuths,
            facade_shade_entity_ids=facade_shades,
            overhang_cutoff_elevation=float(cutoff) if isinstance(cutoff, (int, float)) else None,
        ))
    return tuple(result)


def serialize_zone_config(zone: ZoneConfig) -> dict[str, object]:
    """Persist every configured zone field without silently dropping geometry."""
    return {
        "zone_id": zone.zone_id,
        "name": zone.name,
        "climate_entity_id": zone.climate_entity_id,
        "temperature_entity_id": zone.temperature_entity_id,
        "cooling_power_entity_id": zone.cooling_power_entity_id,
        "comfort_temperature": zone.comfort_temperature,
        "hard_max_temperature": zone.hard_max_temperature,
        "priority": zone.priority,
        "use_climate_temperature_fallback": zone.use_climate_temperature_fallback,
        "shade_entity_ids": list(zone.shade_entity_ids),
        "facade_azimuths": list(zone.facade_azimuths),
        "facade_shade_entity_ids": [list(group) for group in zone.facade_shade_entity_ids],
        "overhang_cutoff_elevation": zone.overhang_cutoff_elevation,
    }


@dataclass(slots=True)
class PVClimateController:
    """Coordinates pure decisions and preserves Shadow Mode."""

    config: ControllerConfig
    command_adapter: ClimateCommandAdapter
    last_decision: ZoneDecision | None = None
    last_ems_grant: EMSGrant | None = None
    last_requested_stages: int = 0
    last_energy: EnergySnapshot = field(default_factory=EnergySnapshot)
    last_house_plan: HousePlan | None = None
    last_zone_forecasts: dict[str, ZoneForecast] = field(default_factory=dict)
    _temperature_samples: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    _mode_samples: dict[str, list[tuple[float, float, str]]] = field(default_factory=dict)
    _thermal_context_samples: dict[str, list[tuple[float, float, str, bool, float | None, float | None, float | None]]] = field(default_factory=dict)
    last_thermal_profiles: dict[str, ThermalProfile] = field(default_factory=dict)
    power_learner: OutdoorPowerLearner = field(default_factory=OutdoorPowerLearner)
    last_power_estimates: dict[str, PowerEstimate] = field(default_factory=dict)
    house_learning: HouseLearningModel = field(default_factory=HouseLearningModel)
    pilot: LivingRoomPilot = field(default_factory=LivingRoomPilot)
    office_pilot: LivingRoomPilot = field(default_factory=lambda: LivingRoomPilot(expected_zone_name="Spielzimmer", display_name="Arbeitszimmer"))
    last_pilot_action: PilotAction | None = None
    last_office_pilot_action: PilotAction | None = None
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
            comfort = float(options.get(CONF_COMFORT_TEMPERATURE, data.get(CONF_COMFORT_TEMPERATURE, 23.5)))
            hard_max = float(options.get(CONF_HARD_MAX_TEMPERATURE, data.get(CONF_HARD_MAX_TEMPERATURE, 25.5)))
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
        zones = _house_zones(options.get(CONF_HOUSE_ZONES))
        if not zones and zone is not None:
            zones = (zone,)
        # A room added through the GUI is already an explicit mapping.  Reuse
        # only an exactly named Wohnzimmer mapping for the one-room pilot;
        # never select an arbitrary first zone.
        if zone is None:
            zone = next((item for item in zones if item.name.strip().casefold() == "wohnzimmer"), None)
        config = ControllerConfig(
            shadow_mode=shadow_mode,
            energy_policy=policy,
            living_room_pilot_enabled=bool(options.get(CONF_LIVING_ROOM_PILOT_ENABLED, data.get(CONF_LIVING_ROOM_PILOT_ENABLED, False))),
            zone=zone,
            ems_granted_stages_entity_id=grant_entity if isinstance(grant_entity, str) else None,
            ems_stale_after_s=float(stale_after),
            pv_power_entity_id=_optional_entity(options, data, CONF_PV_POWER_ENTITY_ID),
            export_power_entity_id=_optional_entity(options, data, CONF_EXPORT_POWER_ENTITY_ID),
            export_power_positive=bool(options.get(CONF_EXPORT_POWER_POSITIVE, data.get(CONF_EXPORT_POWER_POSITIVE, True))),
            pv_forecast_power_entity_id=_optional_entity(options, data, CONF_PV_FORECAST_POWER_ENTITY_ID),
            outdoor_unit_power_entity_id=_optional_entity(options, data, CONF_OUTDOOR_UNIT_POWER_ENTITY_ID),
            min_pv_surplus_w=float(options.get(CONF_MIN_PV_SURPLUS_W, data.get(CONF_MIN_PV_SURPLUS_W, 1000.0))),
            house_zones=zones,
            outdoor_temperature_entity_id=_optional_entity(options, data, CONF_OUTDOOR_TEMPERATURE_ENTITY_ID),
            solar_irradiance_entity_id=_optional_entity(options, data, CONF_SOLAR_IRRADIANCE_ENTITY_ID),
            sun_entity_id=_optional_entity(options, data, CONF_SUN_ENTITY_ID),
        )
        return cls(config=config, command_adapter=ClimateCommandAdapter(shadow_mode=shadow_mode, productive_enabled=config.living_room_pilot_enabled and not shadow_mode))

    def evaluate_house(self, states: Mapping[str, tuple[ZoneInput, str, object]], contexts: Mapping[str, Mapping[str, object]] | None = None) -> HousePlan:
        """Create a read-only common-outdoor-unit plan for every configured zone."""
        telemetry = []
        for zone in self.config.house_zones:
            sample, mode, cooling = states.get(zone.zone_id, (ZoneInput(None, False), "off", None))
            forecast = self._record_forecast(zone, sample.temperature_c)
            thermal_budget = build_thermal_budget(zone, sample.temperature_c, forecast)
            thermal_response = self._record_thermal_response(zone, sample.temperature_c, mode)
            profile = self._record_thermal_profile(zone, sample.temperature_c, mode, (contexts or {}).get(zone.zone_id, {}))
            if profile is not None:
                self.last_thermal_profiles[zone.zone_id] = profile
            try:
                delivered = float(str(cooling))
            except (TypeError, ValueError):
                delivered = None
            telemetry.append(ZoneTelemetry(
                zone_id=zone.zone_id,
                decision=evaluate_zone(
                    zone,
                    sample,
                    now=datetime.now().astimezone().time(),
                    pv_surplus_available=(
                        self.last_energy.export_power_w is not None
                        and self.last_energy.export_power_w >= self.config.min_pv_surplus_w
                    ),
                ),
                hvac_mode=mode,
                delivered_cooling_btu_h=delivered,
                priority=zone.priority,
                name=zone.name,
                temperature_c=sample.temperature_c,
                climate_available=sample.climate_available,
                forecast=forecast,
                temperature_source=sample.temperature_source,
                thermal_budget=thermal_budget,
                thermal_response=thermal_response,
            ))
        self.last_house_plan = build_house_plan(
            HISENSE_5AMW125U4RTA,
            telemetry,
            energy_policy=self.config.energy_policy,
            export_power_w=self.last_energy.export_power_w,
            min_pv_surplus_w=self.config.min_pv_surplus_w,
        )
        return self.last_house_plan

    def observe_outdoor_power(self, active_zone_ids: tuple[str, ...], context: Mapping[str, object] | None = None) -> bool:
        """Learn shared compressor power passively from stable observed modes."""
        now = monotonic()
        captured = self.power_learner.observe(active_zone_ids, self.last_energy.outdoor_unit_power_w, now)
        if captured:
            wall_clock = datetime.now().astimezone()
            values = context or {}
            self.house_learning.observe(
                timestamp=now,
                local_hour=wall_clock.hour,
                active_zone_ids=active_zone_ids,
                outdoor_power_w=self.last_energy.outdoor_unit_power_w,
                pv_power_w=self.last_energy.pv_power_w,
                export_power_w=self.last_energy.export_power_w,
                outdoor_temperature_c=values.get("outdoor_temperature_c") if isinstance(values.get("outdoor_temperature_c"), (int, float)) else None,
                irradiance_w_m2=values.get("irradiance_w_m2") if isinstance(values.get("irradiance_w_m2"), (int, float)) else None,
            )
        self.last_power_estimates = {
            zone.zone_id: self.power_learner.estimate(zone.zone_id, active_zone_ids)
            for zone in self.config.house_zones
            if zone.zone_id not in active_zone_ids
        }
        return captured

    def _record_thermal_profile(self, zone: ZoneConfig, temperature_c: float | None, mode: str, context: Mapping[str, object]) -> ThermalProfile | None:
        if temperature_c is None or not zone.minimum_plausible_temperature_c <= temperature_c <= zone.maximum_plausible_temperature_c:
            return None
        now = monotonic()
        shade = context.get("shade_open_percent")
        outside = context.get("outdoor_temperature_c")
        irradiance = context.get("irradiance_w_m2")
        samples = self._thermal_context_samples.setdefault(zone.zone_id, [])
        record = (
            now,
            temperature_c,
            mode,
            bool(context.get("direct_sun", False)),
            float(shade) if isinstance(shade, (int, float)) else None,
            float(outside) if isinstance(outside, (int, float)) else None,
            float(irradiance) if isinstance(irradiance, (int, float)) else None,
        )
        if not samples or now - samples[-1][0] >= 300 or samples[-1][2:] != record[2:]:
            samples.append(record)
        self._thermal_context_samples[zone.zone_id] = samples = [sample for sample in samples if sample[0] >= now - 7 * 86400]
        return learn_thermal_profile(samples)

    def _record_thermal_response(self, zone: ZoneConfig, temperature_c: float | None, mode: str) -> ThermalResponse | None:
        """Learn only from observed mode states; no device command is involved."""
        if temperature_c is None or not zone.minimum_plausible_temperature_c <= temperature_c <= zone.maximum_plausible_temperature_c:
            return None
        now = monotonic()
        samples = self._mode_samples.setdefault(zone.zone_id, [])
        if not samples or samples[-1][1:] != (temperature_c, mode) or now - samples[-1][0] >= 60:
            samples.append((now, temperature_c, mode))
        cutoff = now - 2 * 3600
        self._mode_samples[zone.zone_id] = samples = [sample for sample in samples if sample[0] >= cutoff]
        return learn_thermal_response(samples)

    def _record_forecast(self, zone: ZoneConfig, temperature_c: float | None) -> ZoneForecast:
        """Keep a bounded in-memory trend; missing data never becomes a forecast."""
        if temperature_c is None:
            forecast = ZoneForecast(zone.zone_id, None, None, 0, "missing")
            self.last_zone_forecasts[zone.zone_id] = forecast
            return forecast
        if not zone.minimum_plausible_temperature_c <= temperature_c <= zone.maximum_plausible_temperature_c:
            forecast = ZoneForecast(zone.zone_id, None, None, 0, "implausible")
            self.last_zone_forecasts[zone.zone_id] = forecast
            return forecast
        now = monotonic()
        samples = self._temperature_samples.setdefault(zone.zone_id, [])
        if not samples or samples[-1][1] != temperature_c or now - samples[-1][0] >= 60:
            samples.append((now, temperature_c))
        cutoff = now - 2 * 3600
        self._temperature_samples[zone.zone_id] = samples = [sample for sample in samples if sample[0] >= cutoff]
        trend = temperature_trend_c_per_h(samples)
        forecast = ZoneForecast(
            zone.zone_id,
            None if trend is None else round(trend, 3),
            None if trend is None else round(predicted_temperature_60m(temperature_c, trend), 2),
            len(samples),
            "valid" if trend is not None else "insufficient_history",
        )
        self.last_zone_forecasts[zone.zone_id] = forecast
        return forecast

    def export_learning_state(self) -> dict[str, object]:
        """Return a secret-free, age-based snapshot safe across restarts."""
        now = monotonic()
        return {
            "temperature_samples": {
                zone_id: [[round(now - timestamp, 3), temperature] for timestamp, temperature in samples if now - timestamp <= 7200]
                for zone_id, samples in self._temperature_samples.items()
            },
            "thermal_context_samples": {
                zone_id: [
                    [round(now - timestamp, 3), temperature, mode, direct_sun, shade, outside, irradiance]
                    for timestamp, temperature, mode, direct_sun, shade, outside, irradiance in samples
                    if now - timestamp <= 7 * 86400
                ]
                for zone_id, samples in self._thermal_context_samples.items()
            },
            "outdoor_power_samples": self.power_learner.export_state(),
            "house_power_observations": self.house_learning.export_state(now),
        }

    def restore_learning_state(self, state: object) -> None:
        """Restore only bounded numeric samples; malformed data is ignored."""
        if not isinstance(state, dict):
            return
        now = monotonic()
        restored: dict[str, list[tuple[float, float]]] = {}
        raw_temperature_samples = state.get("temperature_samples", {})
        for zone_id, samples in raw_temperature_samples.items() if isinstance(raw_temperature_samples, dict) else ():
            if not isinstance(zone_id, str) or not isinstance(samples, list):
                continue
            valid = []
            for sample in samples:
                if not isinstance(sample, list) or len(sample) != 2:
                    continue
                try:
                    age, temperature = float(sample[0]), float(sample[1])
                except (TypeError, ValueError):
                    continue
                if 0 <= age <= 7200:
                    valid.append((now - age, temperature))
            if valid:
                restored[zone_id] = valid
        self._temperature_samples = restored
        restored_context: dict[str, list[tuple[float, float, str, bool, float | None, float | None, float | None]]] = {}
        raw_context_samples = state.get("thermal_context_samples", {})
        for zone_id, samples in raw_context_samples.items() if isinstance(raw_context_samples, dict) else ():
            if not isinstance(zone_id, str) or not isinstance(samples, list):
                continue
            valid_context = []
            for sample in samples:
                if not isinstance(sample, list) or len(sample) != 7 or not isinstance(sample[2], str) or not isinstance(sample[3], bool):
                    continue
                try:
                    age, temperature = float(sample[0]), float(sample[1])
                    shade = None if sample[4] is None else float(sample[4])
                    outside = None if sample[5] is None else float(sample[5])
                    irradiance = None if sample[6] is None else float(sample[6])
                except (TypeError, ValueError):
                    continue
                if 0 <= age <= 7 * 86400:
                    valid_context.append((now - age, temperature, sample[2], sample[3], shade, outside, irradiance))
            if valid_context:
                restored_context[zone_id] = valid_context
        self._thermal_context_samples = restored_context
        self.power_learner.restore_state(state.get("outdoor_power_samples"))
        self.house_learning.restore_state(state.get("house_power_observations"), now)

    @property
    def state(self) -> ControllerState:
        """Return an explicit, fail-safe global state."""
        if self.config.shadow_mode:
            return ControllerState.SHADOW
        if self.config.living_room_pilot_enabled:
            return ControllerState.AUTOMATIC
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
        outdoor_unit_power_state: object = None,
        outdoor_unit_power_unit: object = None,
    ) -> EnergySnapshot:
        """Read configured PV values only; this does not affect a climate device."""
        pv_power = self._power_w(pv_power_state, pv_power_unit) if self.config.pv_power_entity_id else None
        export_power = self._power_w(export_power_state, export_power_unit) if self.config.export_power_entity_id else None
        if export_power is not None and not self.config.export_power_positive:
            export_power *= -1
        forecast = self._power_w(pv_forecast_power_state, pv_forecast_power_unit) if self.config.pv_forecast_power_entity_id else None
        outdoor_power = self._power_w(outdoor_unit_power_state, outdoor_unit_power_unit) if self.config.outdoor_unit_power_entity_id else None
        self.last_energy = EnergySnapshot(pv_power, export_power, forecast, outdoor_power)
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
        outdoor_unit_power_state: object = None,
        outdoor_unit_power_unit: object = None,
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
            outdoor_unit_power_state=outdoor_unit_power_state,
            outdoor_unit_power_unit=outdoor_unit_power_unit,
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
        self.command_adapter.set_operating_mode(shadow_mode=enabled, productive_enabled=self.config.living_room_pilot_enabled and not enabled)

    def set_living_room_pilot_enabled(self, enabled: bool) -> None:
        """Change the explicit GUI pilot gate; no command is sent here."""
        self.config = replace(self.config, living_room_pilot_enabled=enabled)
        self.command_adapter.set_operating_mode(shadow_mode=self.config.shadow_mode, productive_enabled=enabled and not self.config.shadow_mode)

    def request_living_room_pilot_takeover(self) -> None:
        """Queue one explicit handover; the next manual climate change returns control."""
        self.pilot.request_takeover()

    def decide_living_room_pilot(
        self,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
    ) -> PilotAction:
        """Evaluate the only productive PoC route after HA state refresh."""
        if not self.config.living_room_pilot_enabled:
            self.last_pilot_action = PilotAction("none", None, "pilot_disabled", "Wohnzimmer-Pilot ist in der GUI ausgeschaltet.")
            return self.last_pilot_action
        grant = 0 if self.last_ems_grant is None else self.last_ems_grant.stages
        self.last_pilot_action = self.pilot.decide(
            self.config,
            temperature_c=temperature_c,
            climate_mode=climate_mode,
            granted_stages=grant,
            export_power_w=self.last_energy.export_power_w,
            thermal_profile=None if self.config.zone is None else self.last_thermal_profiles.get(self.config.zone.zone_id),
            direct_sun=direct_sun,
            irradiance_w_m2=irradiance_w_m2,
            climate_target_temperature_c=climate_target_temperature_c,
            climate_fan_mode=climate_fan_mode,
            climate_swing_mode=climate_swing_mode,
        )
        return self.last_pilot_action

    def decide_office_pilot(
        self,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
    ) -> PilotAction:
        """Evaluate the productive Arbeitszimmer route only for its exact mapped zone."""
        office_zone = next((zone for zone in self.config.house_zones if zone.name.strip().casefold() == "spielzimmer"), None)
        if not self.config.living_room_pilot_enabled:
            self.last_office_pilot_action = PilotAction("none", None, "pilot_disabled", "Arbeitszimmer-Pilot ist in der GUI ausgeschaltet.")
            return self.last_office_pilot_action
        if office_zone is None:
            self.last_office_pilot_action = PilotAction("none", None, "office_zone_missing", "Arbeitszimmer ist nicht als Zone konfiguriert.")
            return self.last_office_pilot_action
        grant = 0 if self.last_ems_grant is None else self.last_ems_grant.stages
        self.last_office_pilot_action = self.office_pilot.decide(
            replace(self.config, zone=office_zone),
            temperature_c=temperature_c,
            climate_mode=climate_mode,
            granted_stages=grant,
            export_power_w=self.last_energy.export_power_w,
            thermal_profile=self.last_thermal_profiles.get(office_zone.zone_id),
            direct_sun=direct_sun,
            irradiance_w_m2=irradiance_w_m2,
            climate_target_temperature_c=climate_target_temperature_c,
            climate_fan_mode=climate_fan_mode,
            climate_swing_mode=climate_swing_mode,
        )
        return self.last_office_pilot_action

    async def async_apply_pilot_action(self, action: PilotAction, executor, *, zone: ZoneConfig | None = None, room_pilot: LivingRoomPilot | None = None) -> CommandResult:
        """Send a pilot action only through the guarded, rate-limited boundary."""
        target_zone = zone or self.config.zone
        active_pilot = room_pilot or self.pilot
        if action.action not in {"start", "adjust", "stop"} or target_zone is None:
            return CommandResult("noop", action.reason_text)
        command = Command(target_zone.climate_entity_id, f"pilot_{action.action}", action.target_temperature_c)
        result = await self.command_adapter.async_request(command, executor)
        if result.status == "sent":
            active_pilot.mark_sent(action)
        return result

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

    def set_zone_temperature_fallback(self, zone_id: str, enabled: bool) -> None:
        """Enable only an explicit per-zone read fallback; never a device action."""
        zones = tuple(
            replace(zone, use_climate_temperature_fallback=enabled) if zone.zone_id == zone_id else zone
            for zone in self.config.house_zones
        )
        self.config = replace(self.config, house_zones=zones)

    def set_zone_thermal_settings(
        self,
        zone_id: str,
        *,
        comfort_temperature: float | None = None,
        hard_max_temperature: float | None = None,
        priority: int | None = None,
    ) -> None:
        """Change only explicit planning thresholds for one room, never a climate device."""
        updated: list[ZoneConfig] = []
        for zone in self.config.house_zones:
            if zone.zone_id != zone_id:
                updated.append(zone)
                continue
            comfort = zone.comfort_temperature if comfort_temperature is None else float(comfort_temperature)
            hard_max = zone.hard_max_temperature if hard_max_temperature is None else float(hard_max_temperature)
            hard_max = max(comfort, hard_max)
            updated.append(replace(
                zone,
                comfort_temperature=comfort,
                hard_max_temperature=hard_max,
                priority=zone.priority if priority is None else max(1, min(100, int(priority))),
            ))
        self.config = replace(self.config, house_zones=tuple(updated))

    async def async_apply_last_decision(self) -> CommandResult:
        """Demonstrate the sole write boundary; Gate C always blocks it."""
        zone_id = self.config.zone.zone_id if self.config.zone else "unconfigured_zone"
        return await self.command_adapter.async_request(Command(zone_id, "Kühlentscheidung"))
