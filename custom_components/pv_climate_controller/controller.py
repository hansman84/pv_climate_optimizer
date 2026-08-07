"""Runtime controller without a direct Home Assistant write dependency."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from time import monotonic

from .command_adapter import ClimateCommandAdapter, Command, CommandResult
from .const import CONF_BEDROOM_CUTOFF_ENABLED, CONF_BEDROOM_CUTOFF_TIME, CONF_BEDROOM_MODE_ENABLED, CONF_BEDROOM_QUIET_ENABLED, CONF_BEDROOM_QUIET_TIME, CONF_BEDROOM_START_TIME, CONF_BEDROOM_TARGET_TEMPERATURE, CONF_CHILD_BEDROOM_START_TIME, CONF_CLIMATE_ENTITY_ID, CONF_COMFORT_TEMPERATURE, CONF_COOLING_START_OFFSET_C, CONF_EMS_GRANTED_STAGES_ENTITY_ID, CONF_EMS_STALE_AFTER_S, CONF_ENERGY_POLICY, CONF_EXPORT_POWER_ENTITY_ID, CONF_EXPORT_POWER_POSITIVE, CONF_HARD_MAX_TEMPERATURE, CONF_HEAT_PUMP_POWER_ENTITY_ID, CONF_HEAT_PUMP_PRIORITY_ENTITY_ID, CONF_HOT_OUTDOOR_COMFORT_TEMPERATURE, CONF_HOUSE_ZONES, CONF_LIVING_EVENING_COMFORT_TEMPERATURE, CONF_LIVING_EVENING_END_TIME, CONF_LIVING_EVENING_START_TIME, CONF_LIVING_ROOM_PILOT_ENABLED, CONF_MANUAL_OVERRIDE_ENABLED, CONF_MILD_OUTDOOR_COMFORT_TEMPERATURE, CONF_MIN_PV_SURPLUS_W, CONF_NO_PV_HOLD_MAX_POWER_W, CONF_OUTDOOR_TEMPERATURE_ENTITY_ID, CONF_OUTDOOR_UNIT_POWER_ENTITY_ID, CONF_PV_FORECAST_POWER_ENTITY_ID, CONF_PV_POWER_ENTITY_ID, CONF_SHADOW_MODE, CONF_SOLAR_IRRADIANCE_ENTITY_ID, CONF_SUN_ENTITY_ID, CONF_TEMPERATURE_ENTITY_ID, CONF_V2_COOLING_SEASON_ENTITY_ID, CONF_V2_HOUSE_CONTROL_ENABLED, CONF_V2_SHADOW_ENABLED, CONF_V2_VACATION_ENTITY_ID, CONF_ZONE_NAME, ControllerState, EnergyPolicy
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
from .v2_models import CandidateAction, HouseDecision, RoomCandidate, V2CommandPlan, V2RoomInput
from .v2_shadow import V2ShadowRunner
from .v2_authority import AuthorityDecision, HandoffReadiness, RoomAuthorityRegistry
from .v2_command_planner import V2CommandPlanner


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
        normalized_name = " ".join(name.split())
        # The existing Schlafzimmer unit is unreliable. It must never become
        # a productive pilot only because an older configuration is upgraded.
        default_pilot_enabled = normalized_name.casefold() != "schlafzimmer"
        result.append(ZoneConfig(
            zone_id=str(item.get("zone_id", climate)), name=name, climate_entity_id=climate,
            temperature_entity_id=temperature, comfort_temperature=float(item.get("comfort_temperature", 23.5)),
            hard_max_temperature=float(item.get("hard_max_temperature", 25.5)),
            pilot_min_target_temperature=float(item["pilot_min_target_temperature"]) if isinstance(item.get("pilot_min_target_temperature"), (int, float)) else None,
            pilot_max_target_temperature=float(item["pilot_max_target_temperature"]) if isinstance(item.get("pilot_max_target_temperature"), (int, float)) else None,
            hard_limit_failsafe_offset_c=max(0.0, min(8.0, float(item.get("hard_limit_failsafe_offset_c", 1.0)))),
            cooling_power_entity_id=item.get("cooling_power_entity_id") if isinstance(item.get("cooling_power_entity_id"), str) else None,
            priority=int(item.get("priority", 50)),
            modulation_priority=max(1, int(item.get("modulation_priority", 50))),
            pilot_enabled=bool(item.get("pilot_enabled", default_pilot_enabled)),
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
        "pilot_min_target_temperature": zone.pilot_min_target_temperature,
        "pilot_max_target_temperature": zone.pilot_max_target_temperature,
        "hard_limit_failsafe_offset_c": zone.hard_limit_failsafe_offset_c,
        "priority": zone.priority,
        "modulation_priority": zone.modulation_priority,
        "pilot_enabled": zone.pilot_enabled,
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
    speis_pilot: LivingRoomPilot = field(default_factory=lambda: LivingRoomPilot(
        expected_zone_name="Speis",
        display_name="Speis",
        overshoot_margin_c=0.2,
        overshoot_confirmation_s=60,
        thermal_relief_observation_s=5 * 60,
    ))
    bedroom_pilots: dict[str, LivingRoomPilot] = field(default_factory=dict)
    last_pilot_action: PilotAction | None = None
    last_office_pilot_action: PilotAction | None = None
    last_speis_pilot_action: PilotAction | None = None
    last_bedroom_pilot_actions: dict[str, PilotAction] = field(default_factory=dict)
    v2_shadow_runner: V2ShadowRunner = field(default_factory=V2ShadowRunner)
    v2_command_planner: V2CommandPlanner = field(default_factory=V2CommandPlanner)
    last_v2_candidates: tuple[RoomCandidate, ...] = ()
    last_v2_house_decision: HouseDecision | None = None
    last_v2_room_inputs: tuple[V2RoomInput, ...] = ()
    room_authority: RoomAuthorityRegistry = field(default_factory=RoomAuthorityRegistry)
    _last_v2_command_at: dict[str, float] = field(default_factory=dict)
    heat_pump_priority_active: bool = False
    active_cooling_zone_count: int = 0
    effective_living_room_comfort_temperature: float | None = None
    outdoor_comfort_candidate_temperature: float | None = None
    outdoor_comfort_candidate_since: float | None = None
    outdoor_comfort_temperature_c: float | None = None
    effective_bedroom_target_temperature: float | None = None
    bedroom_comfort_candidate_since: float | None = None
    bedroom_comfort_candidate_temperature: float | None = None
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
        # The room profile is the single source of truth for the pilot.  It
        # contains the GUI-visible comfort and hard-limit values; retaining
        # the legacy top-level mapping here would silently apply stale limits.
        living_room_profile = next((item for item in zones if item.name.strip().casefold() == "wohnzimmer"), None)
        if living_room_profile is not None:
            zone = living_room_profile
        configured_minimum_surplus_w = float(options.get(CONF_MIN_PV_SURPLUS_W, data.get(CONF_MIN_PV_SURPLUS_W, 400.0)))
        # Older builds allowed 0 W, which turns an idle meter into a permanent
        # PV approval. Treat that legacy value as invalid configuration rather
        # than silently downgrading the safe default to 100 W after restart.
        minimum_surplus_w = 400.0 if configured_minimum_surplus_w < 100.0 else configured_minimum_surplus_w
        config = ControllerConfig(
            shadow_mode=shadow_mode,
            energy_policy=policy,
            v2_shadow_enabled=bool(options.get(CONF_V2_SHADOW_ENABLED, data.get(CONF_V2_SHADOW_ENABLED, False))) or bool(options.get(CONF_V2_HOUSE_CONTROL_ENABLED, data.get(CONF_V2_HOUSE_CONTROL_ENABLED, False))),
            v2_house_control_enabled=bool(options.get(CONF_V2_HOUSE_CONTROL_ENABLED, data.get(CONF_V2_HOUSE_CONTROL_ENABLED, False))),
            v2_vacation_entity_id=_optional_entity(options, data, CONF_V2_VACATION_ENTITY_ID),
            v2_cooling_season_entity_id=_optional_entity(options, data, CONF_V2_COOLING_SEASON_ENTITY_ID),
            living_room_pilot_enabled=bool(options.get(CONF_LIVING_ROOM_PILOT_ENABLED, data.get(CONF_LIVING_ROOM_PILOT_ENABLED, False))),
            manual_override_enabled=bool(options.get(CONF_MANUAL_OVERRIDE_ENABLED, data.get(CONF_MANUAL_OVERRIDE_ENABLED, True))),
            zone=zone,
            ems_granted_stages_entity_id=grant_entity if isinstance(grant_entity, str) else None,
            ems_stale_after_s=float(stale_after),
            pv_power_entity_id=_optional_entity(options, data, CONF_PV_POWER_ENTITY_ID),
            export_power_entity_id=_optional_entity(options, data, CONF_EXPORT_POWER_ENTITY_ID),
            export_power_positive=bool(options.get(CONF_EXPORT_POWER_POSITIVE, data.get(CONF_EXPORT_POWER_POSITIVE, True))),
            pv_forecast_power_entity_id=_optional_entity(options, data, CONF_PV_FORECAST_POWER_ENTITY_ID),
            outdoor_unit_power_entity_id=_optional_entity(options, data, CONF_OUTDOOR_UNIT_POWER_ENTITY_ID),
            heat_pump_priority_entity_id=_optional_entity(options, data, CONF_HEAT_PUMP_PRIORITY_ENTITY_ID),
            heat_pump_power_entity_id=_optional_entity(options, data, CONF_HEAT_PUMP_POWER_ENTITY_ID),
            min_pv_surplus_w=minimum_surplus_w,
            no_pv_hold_max_power_w=max(0.0, float(options.get(CONF_NO_PV_HOLD_MAX_POWER_W, data.get(CONF_NO_PV_HOLD_MAX_POWER_W, 350.0)))),
            house_zones=zones,
            outdoor_temperature_entity_id=_optional_entity(options, data, CONF_OUTDOOR_TEMPERATURE_ENTITY_ID),
            cooling_start_offset_c=max(0.0, min(3.0, float(options.get(CONF_COOLING_START_OFFSET_C, data.get(CONF_COOLING_START_OFFSET_C, 0.7))))),
            mild_outdoor_comfort_temperature=max(20.0, min(28.0, float(options.get(CONF_MILD_OUTDOOR_COMFORT_TEMPERATURE, data.get(CONF_MILD_OUTDOOR_COMFORT_TEMPERATURE, 25.0))))),
            hot_outdoor_comfort_temperature=max(20.0, min(28.0, float(options.get(CONF_HOT_OUTDOOR_COMFORT_TEMPERATURE, data.get(CONF_HOT_OUTDOOR_COMFORT_TEMPERATURE, 24.0))))),
            living_evening_comfort_temperature=max(20.0, min(28.0, float(options.get(CONF_LIVING_EVENING_COMFORT_TEMPERATURE, data.get(CONF_LIVING_EVENING_COMFORT_TEMPERATURE, 24.5))))),
            living_evening_start_time=str(options.get(CONF_LIVING_EVENING_START_TIME, data.get(CONF_LIVING_EVENING_START_TIME, "20:30"))),
            living_evening_end_time=str(options.get(CONF_LIVING_EVENING_END_TIME, data.get(CONF_LIVING_EVENING_END_TIME, "23:30"))),
            solar_irradiance_entity_id=_optional_entity(options, data, CONF_SOLAR_IRRADIANCE_ENTITY_ID),
            sun_entity_id=_optional_entity(options, data, CONF_SUN_ENTITY_ID),
            bedroom_mode_enabled=bool(options.get(CONF_BEDROOM_MODE_ENABLED, data.get(CONF_BEDROOM_MODE_ENABLED, True))),
            bedroom_cutoff_enabled=bool(options.get(CONF_BEDROOM_CUTOFF_ENABLED, data.get(CONF_BEDROOM_CUTOFF_ENABLED, True))),
            bedroom_start_time=str(options.get(CONF_BEDROOM_START_TIME, data.get(CONF_BEDROOM_START_TIME, "15:30"))),
            child_bedroom_start_time=str(options.get(CONF_CHILD_BEDROOM_START_TIME, data.get(CONF_CHILD_BEDROOM_START_TIME, options.get(CONF_BEDROOM_START_TIME, data.get(CONF_BEDROOM_START_TIME, "15:30"))))),
            bedroom_cutoff_time=str(options.get(CONF_BEDROOM_CUTOFF_TIME, data.get(CONF_BEDROOM_CUTOFF_TIME, "18:30"))),
            bedroom_quiet_enabled=bool(options.get(CONF_BEDROOM_QUIET_ENABLED, data.get(CONF_BEDROOM_QUIET_ENABLED, True))),
            bedroom_quiet_time=str(options.get(CONF_BEDROOM_QUIET_TIME, data.get(CONF_BEDROOM_QUIET_TIME, "18:30"))),
            bedroom_target_temperature=float(options.get(CONF_BEDROOM_TARGET_TEMPERATURE, data.get(CONF_BEDROOM_TARGET_TEMPERATURE, 22.5))),
        )
        if config.v2_house_control_enabled:
            v2_zones = tuple(replace(item, pilot_enabled=False) for item in config.house_zones)
            v2_zone = next((item for item in v2_zones if config.zone is not None and item.zone_id == config.zone.zone_id), config.zone)
            config = replace(
                config,
                living_room_pilot_enabled=False,
                house_zones=v2_zones,
                zone=v2_zone,
            )
        controller = cls(
            config=config,
            command_adapter=ClimateCommandAdapter(
                shadow_mode=False if config.v2_house_control_enabled else shadow_mode,
                productive_enabled=config.v2_house_control_enabled or (config.living_room_pilot_enabled and not shadow_mode),
            ),
        )
        controller._ensure_bedroom_pilots()
        return controller

    def evaluate_v2_shadow(self, rooms: tuple[V2RoomInput, ...], *, available_budget_w: float) -> HouseDecision | None:
        """Evaluate V2 diagnostics only; this method has no command adapter input."""
        if not self.config.v2_shadow_enabled:
            self.last_v2_candidates = ()
            self.last_v2_house_decision = None
            self.last_v2_room_inputs = ()
            return None
        self.last_v2_room_inputs = rooms
        self.last_v2_candidates, self.last_v2_house_decision = self.v2_shadow_runner.evaluate(
            rooms, available_budget_w=available_budget_w
        )
        return self.last_v2_house_decision

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
        self.active_cooling_zone_count = len(set(active_zone_ids))
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
            "pilot_runtime": {
                "wohnzimmer": self.pilot.export_runtime_state(),
                "arbeitszimmer": self.office_pilot.export_runtime_state(),
                "speis": self.speis_pilot.export_runtime_state(),
                "schlafraeume": {zone_id: pilot.export_runtime_state() for zone_id, pilot in self.bedroom_pilots.items()},
            },
            "v2_room_authority": self.room_authority.export_state(),
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
        self.room_authority = RoomAuthorityRegistry.restore(state.get("v2_room_authority"))
        pilot_runtime = state.get("pilot_runtime")
        if isinstance(pilot_runtime, dict):
            self.pilot.restore_runtime_state(pilot_runtime.get("wohnzimmer"))
            self.office_pilot.restore_runtime_state(pilot_runtime.get("arbeitszimmer"))
            self.speis_pilot.restore_runtime_state(pilot_runtime.get("speis"))
            bedrooms = pilot_runtime.get("schlafraeume")
            if isinstance(bedrooms, dict):
                self._ensure_bedroom_pilots()
                for zone_id, room_pilot in self.bedroom_pilots.items():
                    room_pilot.restore_runtime_state(bedrooms.get(zone_id))
        if not self.config.manual_override_enabled:
            self._clear_manual_override_state()

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
        heat_pump_power_state: object = None,
        heat_pump_power_unit: object = None,
        heat_pump_priority_state: object = None,
    ) -> EnergySnapshot:
        """Read configured PV values only; this does not affect a climate device."""
        pv_power = self._power_w(pv_power_state, pv_power_unit) if self.config.pv_power_entity_id else None
        export_power = self._power_w(export_power_state, export_power_unit) if self.config.export_power_entity_id else None
        if export_power is not None and not self.config.export_power_positive:
            export_power *= -1
        forecast = self._power_w(pv_forecast_power_state, pv_forecast_power_unit) if self.config.pv_forecast_power_entity_id else None
        outdoor_power = self._power_w(outdoor_unit_power_state, outdoor_unit_power_unit) if self.config.outdoor_unit_power_entity_id else None
        heat_pump_power = self._power_w(heat_pump_power_state, heat_pump_power_unit) if self.config.heat_pump_power_entity_id else None
        self.last_energy = EnergySnapshot(pv_power, export_power, forecast, outdoor_power, heat_pump_power)
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
        heat_pump_power_state: object = None,
        heat_pump_power_unit: object = None,
        heat_pump_priority_state: object = None,
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
            heat_pump_power_state=heat_pump_power_state,
            heat_pump_power_unit=heat_pump_power_unit,
        )
        self.heat_pump_priority_active = bool(self.config.heat_pump_priority_entity_id and str(heat_pump_priority_state).lower() in {"on", "true", "1"})
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

    def set_v2_shadow_enabled(self, enabled: bool) -> None:
        """Enable only V2 diagnostic comparison; it never changes V1's gate."""
        self.config = replace(self.config, v2_shadow_enabled=enabled)
        if not enabled:
            self.last_v2_candidates = ()
            self.last_v2_house_decision = None
            self.last_v2_room_inputs = ()

    def activate_v2_house_control(self) -> bool:
        """Give V2 sole command ownership for every configured room.

        Ownership changes only after the caller has observed a current climate
        state for every room.  V2 may then leave rooms untouched until the
        orchestrator has an approved plan; V1 is nevertheless blocked from
        issuing competing commands.
        """
        if not self.config.house_zones:
            return False
        activated: list[str] = []
        for zone in self.config.house_zones:
            self.enable_v2_room_shadow(zone.zone_id)
            pending = self.begin_v2_handoff(zone.zone_id, preconditions_met=True)
            if pending.authority.value != "handoff_pending":
                for room_id in activated:
                    self.failback_v2_to_v1(room_id)
                return False
            active = self.activate_v2_authority(zone.zone_id, observed_state_aligned=True)
            if not active.v2_may_write:
                for room_id in activated:
                    self.failback_v2_to_v1(room_id)
                return False
            activated.append(zone.zone_id)
        # A house-wide V2 takeover is also a persistent shutdown of every V1
        # pilot permission.  Authority already prevents V1 writes, but keeping
        # the old switches logically on after a restart is misleading and makes
        # an accidental future reactivation too easy.
        self.config = replace(
            self.config,
            v2_shadow_enabled=True,
            v2_house_control_enabled=True,
            living_room_pilot_enabled=False,
            house_zones=tuple(replace(zone, pilot_enabled=False) for zone in self.config.house_zones),
        )
        # This adapter is still the only service-call boundary.  V1 cannot
        # use it while every room is V2-owned, so the productive permission
        # applies solely to explicitly approved V2 plans.
        self.command_adapter.set_operating_mode(shadow_mode=False, productive_enabled=True)
        return True

    def deactivate_v2_house_control(self) -> None:
        """Start a safe all-room return to V1 without racing pending commands."""
        self.config = replace(self.config, v2_house_control_enabled=False)
        for zone in self.config.house_zones:
            self.begin_v1_rollback(zone.zone_id)
            if "command_ack_pending" not in self.command_adapter.handoff_blockers(zone.climate_entity_id):
                self.complete_v1_rollback(zone.zone_id, observed_state_aligned=True)
        self.command_adapter.set_operating_mode(
            shadow_mode=self.config.shadow_mode,
            productive_enabled=self.config.living_room_pilot_enabled and not self.config.shadow_mode,
        )

    def v2_authority_for(self, zone_id: str) -> AuthorityDecision:
        """Return the visible authority; default ownership is always V1."""
        return self.room_authority.decision_for(zone_id)

    def v2_handoff_readiness(self, zone_id: str) -> HandoffReadiness:
        """Check every precondition without freezing V1 or issuing a command."""
        blockers: list[str] = []
        zone = next((item for item in self.config.house_zones if item.zone_id == zone_id), None)
        authority = self.v2_authority_for(zone_id)
        if zone is None:
            blockers.append("zone_not_configured")
        if not self.config.v2_shadow_enabled:
            blockers.append("v2_shadow_disabled")
        if authority.authority.value != "v2_shadow":
            blockers.append("room_not_in_v2_shadow")
        room_input = next((item for item in self.last_v2_room_inputs if item.policy.room_id == zone_id), None)
        if room_input is None or not room_input.snapshot.critical_inputs_valid:
            blockers.append("critical_inputs_not_fresh")
        candidate = next((item for item in self.last_v2_candidates if item.policy.room_id == zone_id), None)
        if candidate is None or not candidate.requests_modulation:
            blockers.append("v2_candidate_not_actionable")
        if self.last_v2_house_decision is None or zone_id not in self.last_v2_house_decision.approved_room_ids:
            blockers.append("v2_house_step_not_approved")
        if self.v2_command_plan_for(zone_id) is None:
            blockers.append("v2_command_plan_unavailable")
        if zone is not None:
            blockers.extend(self.command_adapter.handoff_blockers(zone.climate_entity_id))
        return HandoffReadiness(zone_id, not blockers, tuple(blockers))

    def v2_command_plan_for(self, zone_id: str) -> V2CommandPlan | None:
        """Return the next V2 plan for dashboard comparison; do not execute it."""
        room = next((item for item in self.last_v2_room_inputs if item.policy.room_id == zone_id), None)
        candidate = next((item for item in self.last_v2_candidates if item.policy.room_id == zone_id), None)
        if room is None or candidate is None or self.last_v2_house_decision is None:
            return None
        return self.v2_command_planner.plan(room, candidate, self.last_v2_house_decision)

    def v2_execution_order(self) -> tuple[str, ...]:
        """Return actionable rooms fairly for the one-command shared transport.

        The adapter intentionally serializes cloud calls.  Keeping config order
        here would let an always-changing early room consume every available
        minute, so the room whose last successful V2 step is oldest goes first.
        This changes neither the house budget nor any device safety interval.
        """
        return tuple(
            zone.zone_id
            for zone in sorted(
                self.config.house_zones,
                key=lambda zone: self._last_v2_command_at.get(zone.zone_id, float("-inf")),
            )
        )

    def enable_v2_room_shadow(self, zone_id: str) -> AuthorityDecision:
        """Mark one room for V2 comparison only; V1 remains its sole writer."""
        return self.room_authority.enable_shadow(zone_id)

    def disable_v2_room_shadow(self, zone_id: str) -> AuthorityDecision:
        """Return an unhanded room from comparison ownership to ordinary V1."""
        return self.room_authority.disable_shadow(zone_id)

    def begin_v2_handoff(self, zone_id: str, *, preconditions_met: bool) -> AuthorityDecision:
        """Freeze both paths while a future UI verifies state adoption."""
        return self.room_authority.begin_handoff(zone_id, preconditions_met=preconditions_met)

    def activate_v2_authority(self, zone_id: str, *, observed_state_aligned: bool) -> AuthorityDecision:
        """Complete a handoff only after adopting the observed device state."""
        return self.room_authority.activate_v2(zone_id, observed_state_aligned=observed_state_aligned)

    def begin_v1_rollback(self, zone_id: str) -> AuthorityDecision:
        """Freeze both paths before returning a room to V1."""
        return self.room_authority.begin_rollback(zone_id)

    def complete_v1_rollback(self, zone_id: str, *, observed_state_aligned: bool) -> AuthorityDecision:
        """Return V1 authority only after it adopts the observed device state."""
        return self.room_authority.complete_rollback(zone_id, observed_state_aligned=observed_state_aligned)

    def failback_v2_to_v1(self, zone_id: str) -> AuthorityDecision:
        """Immediately restore V1 after a V2 transport failure.

        No device command is issued here: the current observed state is kept and
        V1 resumes from the next decision in the same refresh cycle.
        """
        pending = self.begin_v1_rollback(zone_id)
        if pending.authority.value != "rollback_pending":
            return pending
        return self.complete_v1_rollback(zone_id, observed_state_aligned=True)

    def set_living_room_pilot_enabled(self, enabled: bool) -> None:
        """Change the explicit GUI pilot gate; no command is sent here."""
        self.config = replace(self.config, living_room_pilot_enabled=enabled)
        # V1 may be deliberately disabled while V2 owns the whole house.  The
        # shared adapter must remain available to V2 in that state, otherwise
        # turning the retained V1 master switch off would silently stop V2.
        v2_owns_house = self.config.v2_house_control_enabled
        self.command_adapter.set_operating_mode(
            shadow_mode=False if v2_owns_house else self.config.shadow_mode,
            productive_enabled=v2_owns_house or (enabled and not self.config.shadow_mode),
        )

    def set_manual_override_enabled(self, enabled: bool) -> None:
        """Choose whether a HA user's climate change may release a pilot."""
        self.config = replace(self.config, manual_override_enabled=enabled)
        if not enabled:
            self._clear_manual_override_state()

    def _clear_manual_override_state(self) -> None:
        """Return every permitted room to pilot ownership immediately."""
        self._ensure_bedroom_pilots()
        for room_pilot in (self.pilot, self.office_pilot, self.speis_pilot, *self.bedroom_pilots.values()):
            room_pilot.request_takeover()

    def set_bedroom_mode_enabled(self, enabled: bool) -> None:
        """Enable or pause only the scheduled sleeping-room strategy."""
        self.config = replace(self.config, bedroom_mode_enabled=enabled)

    def set_bedroom_cutoff_enabled(self, enabled: bool) -> None:
        """Allow the user to make the evening hard stop optional."""
        self.config = replace(self.config, bedroom_cutoff_enabled=enabled)

    def set_bedroom_quiet_enabled(self, enabled: bool) -> None:
        """Enable the independently scheduled bedroom quiet time."""
        self.config = replace(self.config, bedroom_quiet_enabled=enabled)

    def set_bedroom_quiet_time(self, quiet_time: str) -> None:
        """Persist the bedroom-only quiet-time boundary."""
        self.config = replace(self.config, bedroom_quiet_time=quiet_time)

    def set_bedroom_schedule(self, *, start_time: str | None = None, cutoff_time: str | None = None) -> None:
        """Keep schedule changes GUI-persistent and constrained by select options."""
        self.config = replace(
            self.config,
            bedroom_start_time=self.config.bedroom_start_time if start_time is None else start_time,
            bedroom_cutoff_time=self.config.bedroom_cutoff_time if cutoff_time is None else cutoff_time,
        )

    def set_child_bedroom_start_time(self, start_time: str) -> None:
        """Persist Kinderzimmer PV pre-cooling independently from Schlafzimmer."""
        self.config = replace(self.config, child_bedroom_start_time=start_time)

    def set_bedroom_target_temperature(self, value: float) -> None:
        """Set the thermal promise for both sleeping rooms without altering daytime comfort."""
        self.config = replace(self.config, bedroom_target_temperature=min(25.0, max(20.0, value)))

    def set_zone_pilot_enabled(self, zone_id: str, enabled: bool) -> None:
        """Grant or revoke productive pilot control for exactly one room."""
        zones = tuple(
            replace(zone, pilot_enabled=enabled) if zone.zone_id == zone_id else zone
            for zone in self.config.house_zones
        )
        selected_zone = self.config.zone
        if selected_zone is not None:
            selected_zone = next((zone for zone in zones if zone.zone_id == selected_zone.zone_id), selected_zone)
        self.config = replace(self.config, house_zones=zones, zone=selected_zone)

    def request_living_room_pilot_takeover(self) -> None:
        """Queue one explicit handover; the next manual climate change returns control."""
        self.pilot.request_takeover()

    def request_office_pilot_takeover(self) -> None:
        """Queue an explicit Arbeitszimmer handover with the same safety boundary."""
        self.office_pilot.request_takeover()

    def request_speis_pilot_takeover(self) -> None:
        """Queue an explicit Speis handover with its tighter thermal guard."""
        self.speis_pilot.request_takeover()

    def _ensure_bedroom_pilots(self) -> None:
        """Create isolated pilots only for the two explicitly named sleeping rooms."""
        for zone in self.config.house_zones:
            if zone.name.strip().casefold() not in {"schlafzimmer", "kinderzimmer"}:
                continue
            self.bedroom_pilots.setdefault(
                zone.zone_id,
                LivingRoomPilot(
                    expected_zone_name=zone.name,
                    display_name=zone.name,
                    min_start_target_c=22.0,
                    max_start_target_c=23.0,
                    thermal_relief_target_c=24.0,
                ),
            )

    @staticmethod
    def _schedule_time(value: str, fallback: time) -> time:
        """Parse persisted HH:MM values defensively."""
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
            return time(hour, minute)
        except (AttributeError, TypeError, ValueError):
            return fallback

    def decide_bedroom_pilot(
        self,
        zone: ZoneConfig,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
        manual_change_candidate: bool = True,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
        shade_open_percent: float | None = None,
        outdoor_temperature_c: float | None = None,
        now: time | None = None,
    ) -> PilotAction:
        """Use late-afternoon PV for sleeping rooms and enforce their quiet time."""
        self._ensure_bedroom_pilots()
        pilot = self.bedroom_pilots.get(zone.zone_id)
        if pilot is None:
            return PilotAction("none", None, "bedroom_zone_missing", "Schlafraum ist nicht als Pilotzone konfiguriert.")
        if not self.config.living_room_pilot_enabled or not self.config.bedroom_mode_enabled:
            action = PilotAction("none", None, "bedroom_mode_disabled", "Schlafraum-Modus ist in der GUI ausgeschaltet.")
            self.last_bedroom_pilot_actions[zone.zone_id] = action
            return action
        if not zone.pilot_enabled:
            action = PilotAction("none", None, "zone_pilot_disabled", f"{zone.name}-Pilot ist für diesen Raum ausgeschaltet.")
            self.last_bedroom_pilot_actions[zone.zone_id] = action
            return action
        local_time = now or datetime.now().astimezone().time()
        is_master_bedroom = zone.name.strip().casefold() == "schlafzimmer"
        start_value = self.config.bedroom_start_time if is_master_bedroom else self.config.child_bedroom_start_time
        start = self._schedule_time(start_value, time(15, 30))
        quiet_enabled = self.config.bedroom_quiet_enabled if is_master_bedroom else self.config.bedroom_cutoff_enabled
        quiet_value = self.config.bedroom_quiet_time if is_master_bedroom else self.config.bedroom_cutoff_time
        cutoff = self._schedule_time(quiet_value, time(18, 30))
        # A room-specific cutoff is the end of that room's pre-cooling run.
        # It is deliberately a hard stop, even when the room is still warm:
        # otherwise the deadline is not a deadline and the unit can run far
        # into the evening merely because the temperature has not converged.
        if quiet_enabled and local_time >= cutoff:
            action = (
                PilotAction("stop", None, "bedroom_quiet_time", f"{zone.name}: Ruhezeit ab {cutoff.strftime('%H:%M')} Uhr; Klimagerät wird ausgeschaltet.")
                if climate_mode == "cool"
                else PilotAction("none", None, "bedroom_quiet_time", f"{zone.name}: Ruhezeit ab {cutoff.strftime('%H:%M')} Uhr aktiv.")
            )
            self.last_bedroom_pilot_actions[zone.zone_id] = action
            return action
        target_zone = replace(zone, comfort_temperature=self._effective_bedroom_target(outdoor_temperature_c))
        if local_time < start:
            action = PilotAction("none", None, "bedroom_window_pending", f"{zone.name}: PV-Vorkühlung beginnt ab {start.strftime('%H:%M')} Uhr.")
            self.last_bedroom_pilot_actions[zone.zone_id] = action
            return action
        forecast = self.last_zone_forecasts.get(zone.zone_id)
        grant = 0 if self.last_ems_grant is None else self.last_ems_grant.stages
        action = pilot.decide(
            replace(self.config, zone=target_zone),
            temperature_c=temperature_c,
            climate_mode=climate_mode,
            granted_stages=grant,
            export_power_w=self.last_energy.export_power_w,
            outdoor_unit_power_w=self.last_energy.outdoor_unit_power_w,
            heat_pump_priority_active=self.heat_pump_priority_active,
            heat_pump_power_w=self.last_energy.heat_pump_power_w,
            heat_pump_relief_step_interval_s=60.0 * max(1, self.active_cooling_zone_count),
            thermal_profile=self.last_thermal_profiles.get(zone.zone_id),
            temperature_trend_c_per_h=None if forecast is None else forecast.trend_c_per_h,
            predicted_temperature_60m_c=None if forecast is None else forecast.predicted_temperature_60m_c,
            direct_sun=direct_sun,
            irradiance_w_m2=irradiance_w_m2,
            shade_open_percent=shade_open_percent,
            active_cooling_zone_count=self.active_cooling_zone_count,
            climate_target_temperature_c=climate_target_temperature_c,
            climate_fan_mode=climate_fan_mode,
            climate_swing_mode=climate_swing_mode,
            pv_deadline_active=True,
            comfort_required=True,
            manual_change_candidate=manual_change_candidate,
        )
        self.last_bedroom_pilot_actions[zone.zone_id] = action
        return action

    def decide_living_room_pilot(
        self,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
        pv_deadline_active: bool = False,
        manual_change_candidate: bool = True,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
        shade_open_percent: float | None = None,
        outdoor_temperature_c: float | None = None,
        now: time | None = None,
    ) -> PilotAction:
        """Evaluate the only productive PoC route after HA state refresh."""
        if not self.config.living_room_pilot_enabled:
            self.last_pilot_action = PilotAction("none", None, "pilot_disabled", "Wohnzimmer-Pilot ist in der GUI ausgeschaltet.")
            return self.last_pilot_action
        if self.config.zone is not None and not self.config.zone.pilot_enabled:
            self.last_pilot_action = PilotAction("none", None, "zone_pilot_disabled", "Wohnzimmer-Pilot ist für diesen Raum ausgeschaltet.")
            return self.last_pilot_action
        grant = 0 if self.last_ems_grant is None else self.last_ems_grant.stages
        evening_comfort_active = self.living_evening_comfort_active(now)
        effective_zone = self._effective_living_room_zone(outdoor_temperature_c, now=now)
        forecast = None if effective_zone is None else self.last_zone_forecasts.get(effective_zone.zone_id)
        self.last_pilot_action = self.pilot.decide(
            replace(self.config, zone=effective_zone),
            temperature_c=temperature_c,
            climate_mode=climate_mode,
            granted_stages=grant,
            export_power_w=self.last_energy.export_power_w,
            outdoor_unit_power_w=self.last_energy.outdoor_unit_power_w,
            heat_pump_priority_active=self.heat_pump_priority_active,
            heat_pump_power_w=self.last_energy.heat_pump_power_w,
            heat_pump_relief_step_interval_s=60.0 * max(1, self.active_cooling_zone_count),
            thermal_profile=None if effective_zone is None else self.last_thermal_profiles.get(effective_zone.zone_id),
            temperature_trend_c_per_h=None if forecast is None else forecast.trend_c_per_h,
            predicted_temperature_60m_c=None if forecast is None else forecast.predicted_temperature_60m_c,
            direct_sun=direct_sun,
            irradiance_w_m2=irradiance_w_m2,
            shade_open_percent=shade_open_percent,
            active_cooling_zone_count=self.active_cooling_zone_count,
            climate_target_temperature_c=climate_target_temperature_c,
            climate_fan_mode=climate_fan_mode,
            climate_swing_mode=climate_swing_mode,
            pv_deadline_active=pv_deadline_active,
            comfort_required=evening_comfort_active,
            manual_change_candidate=manual_change_candidate,
        )
        return self.last_pilot_action

    def living_evening_comfort_active(self, now: time | None = None) -> bool:
        """Return whether the configured occupied-evening comfort window is active."""
        local_time = now or datetime.now().astimezone().time()
        start = self._schedule_time(self.config.living_evening_start_time, time(20, 30))
        end = self._schedule_time(self.config.living_evening_end_time, time(23, 30))
        if start <= end:
            return start <= local_time < end
        return local_time >= start or local_time < end

    def _effective_living_room_zone(
        self,
        outdoor_temperature_c: float | None,
        zone: ZoneConfig | None = None,
        *,
        now: time | None = None,
    ) -> ZoneConfig | None:
        """Apply the confirmed outdoor comfort band without treating outside air as ventilation.

        The profile only relaxes the desired room temperature. Direct sun, room
        temperature and the hard limit remain fully effective safeguards.
        A band must persist for 15 minutes before becoming active.
        """
        zone = self.config.zone if zone is None else zone
        if zone is None or zone.name.strip().casefold() not in {"wohnzimmer", "spielzimmer"}:
            return zone
        self.outdoor_comfort_temperature_c = outdoor_temperature_c
        base_temperature = zone.comfort_temperature
        if zone.name.strip().casefold() == "wohnzimmer" and self.living_evening_comfort_active(now):
            self.effective_living_room_comfort_temperature = self.config.living_evening_comfort_temperature
            self.outdoor_comfort_candidate_since = None
            return replace(zone, comfort_temperature=self.config.living_evening_comfort_temperature)
        candidate = base_temperature if outdoor_temperature_c is None else (
            self.config.mild_outdoor_comfort_temperature if outdoor_temperature_c <= 28.0
            else self.config.hot_outdoor_comfort_temperature
        )
        now = monotonic()
        if candidate != self.outdoor_comfort_candidate_temperature:
            self.outdoor_comfort_candidate_temperature = candidate
            self.outdoor_comfort_candidate_since = now
        if self.effective_living_room_comfort_temperature is None:
            self.effective_living_room_comfort_temperature = base_temperature
        if candidate == self.effective_living_room_comfort_temperature:
            self.outdoor_comfort_candidate_since = None
        elif self.outdoor_comfort_candidate_since is not None and now - self.outdoor_comfort_candidate_since >= 15 * 60:
            self.effective_living_room_comfort_temperature = candidate
            self.outdoor_comfort_candidate_since = None
        return replace(zone, comfort_temperature=self.effective_living_room_comfort_temperature)

    def living_room_outdoor_comfort_status(self) -> dict[str, float | int | str | None]:
        """Return the complete, dashboard-friendly state of the comfort profile."""
        zone = self.config.zone
        base = None if zone is None else zone.comfort_temperature
        active = self.effective_living_room_comfort_temperature
        candidate = self.outdoor_comfort_candidate_temperature
        pending_s = 0
        if self.outdoor_comfort_candidate_since is not None:
            pending_s = max(0, int(15 * 60 - (monotonic() - self.outdoor_comfort_candidate_since)))
        if self.outdoor_comfort_temperature_c is None:
            state = "Außentemperatur fehlt – Grundkomfort aktiv."
        elif pending_s:
            state = f"Außenband wird noch {max(1, (pending_s + 59) // 60)} Minute(n) bestätigt."
        elif active == base:
            state = "Hitzetag – Grundkomfort aktiv."
        else:
            state = "Außenkomfort für Wohn- und Arbeitszimmer aktiv; Außenluft wird nicht als Kühlung angenommen."
        return {
            "state": state,
            "outdoor_temperature_c": self.outdoor_comfort_temperature_c,
            "base_comfort_temperature_c": base,
            "effective_comfort_temperature_c": active,
            "candidate_comfort_temperature_c": candidate,
            "stability_remaining_s": pending_s,
            "stability_required_s": 15 * 60,
        }

    def _effective_bedroom_target(self, outdoor_temperature_c: float | None) -> float:
        """Relax sleeping-room pre-cooling to a 23 °C evening target off hot days."""
        self.outdoor_comfort_temperature_c = outdoor_temperature_c
        base_target = self.config.bedroom_target_temperature
        candidate = 23.0 if outdoor_temperature_c is not None and outdoor_temperature_c <= 28.0 else base_target
        now = monotonic()
        if candidate != self.bedroom_comfort_candidate_temperature:
            self.bedroom_comfort_candidate_temperature = candidate
            self.bedroom_comfort_candidate_since = now
        if self.effective_bedroom_target_temperature is None:
            self.effective_bedroom_target_temperature = base_target
        if candidate == self.effective_bedroom_target_temperature:
            self.bedroom_comfort_candidate_since = None
        elif self.bedroom_comfort_candidate_since is not None and now - self.bedroom_comfort_candidate_since >= 15 * 60:
            self.effective_bedroom_target_temperature = candidate
            self.bedroom_comfort_candidate_since = None
        return self.effective_bedroom_target_temperature

    def bedroom_outdoor_comfort_status(self) -> dict[str, float | int | str | None]:
        """Expose the evening target and its 15-minute confirmation state."""
        pending_s = 0
        if self.bedroom_comfort_candidate_since is not None:
            pending_s = max(0, int(15 * 60 - (monotonic() - self.bedroom_comfort_candidate_since)))
        active = self.effective_bedroom_target_temperature or self.config.bedroom_target_temperature
        if self.outdoor_comfort_temperature_c is None:
            state = "Außentemperatur fehlt – bisheriges Abendziel aktiv."
        elif pending_s:
            state = f"Entspannteres Abendziel wird noch {max(1, (pending_s + 59) // 60)} Minute(n) bestätigt."
        elif active == self.config.bedroom_target_temperature:
            state = "Hitzetag – bisherige Vorkühlung aktiv."
        else:
            state = "Gemäßigte Außenlage – Abendziel 23 °C aktiv."
        return {
            "state": state,
            "outdoor_temperature_c": self.outdoor_comfort_temperature_c,
            "base_evening_target_temperature_c": self.config.bedroom_target_temperature,
            "effective_evening_target_temperature_c": active,
            "candidate_evening_target_temperature_c": self.bedroom_comfort_candidate_temperature,
            "stability_remaining_s": pending_s,
            "stability_required_s": 15 * 60,
        }

    def decide_office_pilot(
        self,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
        pv_deadline_active: bool = False,
        manual_change_candidate: bool = True,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
        shade_open_percent: float | None = None,
        outdoor_temperature_c: float | None = None,
    ) -> PilotAction:
        """Evaluate the productive Arbeitszimmer route only for its exact mapped zone."""
        office_zone = next((zone for zone in self.config.house_zones if zone.name.strip().casefold() == "spielzimmer"), None)
        if not self.config.living_room_pilot_enabled:
            self.last_office_pilot_action = PilotAction("none", None, "pilot_disabled", "Arbeitszimmer-Pilot ist in der GUI ausgeschaltet.")
            return self.last_office_pilot_action
        if office_zone is None:
            self.last_office_pilot_action = PilotAction("none", None, "office_zone_missing", "Arbeitszimmer ist nicht als Zone konfiguriert.")
            return self.last_office_pilot_action
        if not office_zone.pilot_enabled:
            self.last_office_pilot_action = PilotAction("none", None, "zone_pilot_disabled", "Arbeitszimmer-Pilot ist für diesen Raum ausgeschaltet.")
            return self.last_office_pilot_action
        grant = 0 if self.last_ems_grant is None else self.last_ems_grant.stages
        effective_zone = self._effective_living_room_zone(outdoor_temperature_c, office_zone)
        forecast = self.last_zone_forecasts.get(office_zone.zone_id)
        self.last_office_pilot_action = self.office_pilot.decide(
            replace(self.config, zone=effective_zone),
            temperature_c=temperature_c,
            climate_mode=climate_mode,
            granted_stages=grant,
            export_power_w=self.last_energy.export_power_w,
            outdoor_unit_power_w=self.last_energy.outdoor_unit_power_w,
            heat_pump_priority_active=self.heat_pump_priority_active,
            heat_pump_power_w=self.last_energy.heat_pump_power_w,
            heat_pump_relief_step_interval_s=60.0 * max(1, self.active_cooling_zone_count),
            thermal_profile=self.last_thermal_profiles.get(office_zone.zone_id),
            temperature_trend_c_per_h=None if forecast is None else forecast.trend_c_per_h,
            predicted_temperature_60m_c=None if forecast is None else forecast.predicted_temperature_60m_c,
            direct_sun=direct_sun,
            irradiance_w_m2=irradiance_w_m2,
            shade_open_percent=shade_open_percent,
            active_cooling_zone_count=self.active_cooling_zone_count,
            climate_target_temperature_c=climate_target_temperature_c,
            climate_fan_mode=climate_fan_mode,
            climate_swing_mode=climate_swing_mode,
            pv_deadline_active=pv_deadline_active,
            manual_change_candidate=manual_change_candidate,
        )
        return self.last_office_pilot_action

    def decide_speis_pilot(
        self,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
        pv_deadline_active: bool = False,
        manual_change_candidate: bool = True,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
        shade_open_percent: float | None = None,
    ) -> PilotAction:
        """Evaluate the small Speis as a productive zone with a fast overshoot guard."""
        speis_zone = next((zone for zone in self.config.house_zones if zone.name.strip().casefold() == "speis"), None)
        if not self.config.living_room_pilot_enabled:
            self.last_speis_pilot_action = PilotAction("none", None, "pilot_disabled", "Speis-Pilot ist in der GUI ausgeschaltet.")
            return self.last_speis_pilot_action
        if speis_zone is None:
            self.last_speis_pilot_action = PilotAction("none", None, "speis_zone_missing", "Speis ist nicht als Zone konfiguriert.")
            return self.last_speis_pilot_action
        if not speis_zone.pilot_enabled:
            self.last_speis_pilot_action = PilotAction("none", None, "zone_pilot_disabled", "Speis-Pilot ist für diesen Raum ausgeschaltet.")
            return self.last_speis_pilot_action
        grant = 0 if self.last_ems_grant is None else self.last_ems_grant.stages
        forecast = self.last_zone_forecasts.get(speis_zone.zone_id)
        self.last_speis_pilot_action = self.speis_pilot.decide(
            replace(self.config, zone=speis_zone),
            temperature_c=temperature_c,
            climate_mode=climate_mode,
            granted_stages=grant,
            export_power_w=self.last_energy.export_power_w,
            outdoor_unit_power_w=self.last_energy.outdoor_unit_power_w,
            heat_pump_priority_active=self.heat_pump_priority_active,
            heat_pump_power_w=self.last_energy.heat_pump_power_w,
            heat_pump_relief_step_interval_s=60.0 * max(1, self.active_cooling_zone_count),
            thermal_profile=self.last_thermal_profiles.get(speis_zone.zone_id),
            temperature_trend_c_per_h=None if forecast is None else forecast.trend_c_per_h,
            predicted_temperature_60m_c=None if forecast is None else forecast.predicted_temperature_60m_c,
            direct_sun=direct_sun,
            irradiance_w_m2=irradiance_w_m2,
            shade_open_percent=shade_open_percent,
            active_cooling_zone_count=self.active_cooling_zone_count,
            climate_target_temperature_c=climate_target_temperature_c,
            climate_fan_mode=climate_fan_mode,
            climate_swing_mode=climate_swing_mode,
            pv_deadline_active=pv_deadline_active,
            manual_change_candidate=manual_change_candidate,
        )
        return self.last_speis_pilot_action

    async def async_apply_pilot_action(self, action: PilotAction, executor, *, zone: ZoneConfig | None = None, room_pilot: LivingRoomPilot | None = None) -> CommandResult:
        """Send a pilot action only through the guarded, rate-limited boundary."""
        target_zone = zone or self.config.zone
        active_pilot = room_pilot or self.pilot
        if action.action not in {"start", "adjust", "stop"} or target_zone is None:
            return CommandResult("noop", action.reason_text)
        authority = self.v2_authority_for(target_zone.zone_id)
        if not authority.v1_may_write:
            # During handoff and rollback neither controller may create a
            # command.  When V2 later becomes active this is the V1 half of
            # the single-writer guarantee, ahead of the shared adapter.
            return CommandResult("authority_blocked", authority.reason_text)
        # Wärmepumpenentlastung advances only one indoor-unit degree per
        # minute. Urgency bypasses the five-minute per-device cadence, while
        # the shared adapter preserves the one-command-per-minute house ramp.
        urgent_reasons = {
            "heat_pump_priority_relief_step",
            "heat_pump_priority_recovery_step",
            "heat_pump_priority_comfort_guard",
            "hard_temperature_limit_failsafe",
            "pv_comfort_recovery",
            "pv_wind_down",
        }
        command = Command(
            target_zone.climate_entity_id,
            f"pilot_{action.action}",
            action.target_temperature_c,
            urgent=action.reason_code in urgent_reasons,
        )
        if action.reason_code == "pilot_target_drift":
            # This path is reached only after the pilot compared the desired
            # target with the reported device target beyond its ack grace.
            # Let every room re-send that exact command through the normal
            # rate-limited boundary instead of treating an old send as proof.
            self.command_adapter.invalidate_confirmed_signature(command)
        result = await self.command_adapter.async_request(command, executor)
        if result.status == "sent":
            active_pilot.mark_sent(action)
        return result

    async def async_apply_v2_command(self, plan: V2CommandPlan, executor) -> CommandResult:
        """Use V1's sole adapter and supplied executor after explicit authority.

        This method does not make a service call itself.  It is deliberately
        unavailable in V2 Shadow and during handoff/rollback, so a future V2
        executor cannot become a second writer accidentally.
        """
        zone = next((item for item in self.config.house_zones if item.zone_id == plan.room_id), None)
        if zone is None and self.config.zone is not None and self.config.zone.zone_id == plan.room_id:
            zone = self.config.zone
        if zone is None:
            return CommandResult("invalid", "V2-Befehl blockiert: Raum ist nicht konfiguriert.")
        authority = self.v2_authority_for(plan.room_id)
        if not authority.v2_may_write:
            return CommandResult("authority_blocked", authority.reason_text)
        now = monotonic()
        last_command_at = self._last_v2_command_at.get(plan.room_id)
        if last_command_at is not None and now - last_command_at < 2 * 60:
            remaining_s = int(2 * 60 - (now - last_command_at))
            return CommandResult(
                "backoff",
                f"V2 beobachtet {zone.name} noch {remaining_s // 60 + 1} Min. nach der letzten Sollwertstufe.",
            )
        action = {
            CandidateAction.START: "pilot_start",
            CandidateAction.ADJUST: "pilot_adjust",
            CandidateAction.STOP: "pilot_stop",
        }[plan.action]
        command = Command(
            zone.climate_entity_id,
            action,
            plan.target_temperature_c,
            urgent=True,
            fan_mode="auto" if plan.action is not CandidateAction.STOP else None,
            batch_window=True,
        )
        # ConnectLife can accept a command yet later report its previous
        # target again.  A remembered signature must not turn that stale
        # report into a permanent V2 no-op.  The V2 two-minute observation
        # window above still limits the retry, so this only reasserts the
        # approved target when the device demonstrably drifted from it.
        if (
            plan.action is CandidateAction.ADJUST
            and plan.target_temperature_c is not None
            and any(
                room.policy.room_id == plan.room_id
                and room.observed_target_temperature_c != plan.target_temperature_c
                for room in self.last_v2_room_inputs
            )
        ):
            self.command_adapter.invalidate_confirmed_signature(command)
        result = await self.command_adapter.async_request(command, executor)
        if result.status == "sent":
            self._last_v2_command_at[plan.room_id] = now
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
        self.config = replace(self.config, min_pv_surplus_w=max(100.0, watts))

    def set_no_pv_hold_max_power_w(self, watts: float) -> None:
        """Set the measured-power ceiling for a deliberate no-PV hold."""
        self.config = replace(self.config, no_pv_hold_max_power_w=max(0.0, watts))

    def set_cooling_start_offset_c(self, value: float) -> None:
        """Require a visible external-room-temperature margin before PV cooling starts."""
        self.config = replace(self.config, cooling_start_offset_c=max(0.0, min(3.0, value)))

    def set_outdoor_comfort_temperature(self, *, mild: float | None = None, hot: float | None = None) -> None:
        """Update the visible day-room comfort profile without touching the Speis."""
        self.config = replace(
            self.config,
            mild_outdoor_comfort_temperature=self.config.mild_outdoor_comfort_temperature if mild is None else max(20.0, min(28.0, mild)),
            hot_outdoor_comfort_temperature=self.config.hot_outdoor_comfort_temperature if hot is None else max(20.0, min(28.0, hot)),
        )

    def set_living_evening_comfort_temperature(self, value: float) -> None:
        self.config = replace(self.config, living_evening_comfort_temperature=max(20.0, min(28.0, value)))

    def set_living_evening_schedule(self, *, start_time: str | None = None, end_time: str | None = None) -> None:
        """Update the occupied-evening window exposed by integration controls."""
        self.config = replace(
            self.config,
            living_evening_start_time=self.config.living_evening_start_time if start_time is None else start_time,
            living_evening_end_time=self.config.living_evening_end_time if end_time is None else end_time,
        )


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
        pilot_min_target_temperature: float | None = None,
        pilot_max_target_temperature: float | None = None,
        hard_limit_failsafe_offset_c: float | None = None,
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
            pilot_min = zone.pilot_min_target_temperature if pilot_min_target_temperature is None else max(16.0, min(32.0, float(pilot_min_target_temperature)))
            pilot_max = zone.pilot_max_target_temperature if pilot_max_target_temperature is None else max(16.0, min(32.0, float(pilot_max_target_temperature)))
            failsafe_offset = zone.hard_limit_failsafe_offset_c if hard_limit_failsafe_offset_c is None else max(0.0, min(8.0, float(hard_limit_failsafe_offset_c)))
            if pilot_min is not None and pilot_max is not None:
                pilot_max = max(pilot_min, pilot_max)
            updated.append(replace(
                zone,
                comfort_temperature=comfort,
                hard_max_temperature=hard_max,
                pilot_min_target_temperature=pilot_min,
                pilot_max_target_temperature=pilot_max,
                hard_limit_failsafe_offset_c=failsafe_offset,
                priority=zone.priority if priority is None else max(1, min(100, int(priority))),
            ))
        zones = tuple(updated)
        selected_zone = self.config.zone
        if selected_zone is not None:
            selected_zone = next((zone for zone in zones if zone.zone_id == selected_zone.zone_id), selected_zone)
        self.config = replace(self.config, house_zones=zones, zone=selected_zone)

    async def async_apply_last_decision(self) -> CommandResult:
        """Demonstrate the sole write boundary; Gate C always blocks it."""
        zone_id = self.config.zone.zone_id if self.config.zone else "unconfigured_zone"
        return await self.command_adapter.async_request(Command(zone_id, "Kühlentscheidung"))
