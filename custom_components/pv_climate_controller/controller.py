"""Runtime controller without a direct Home Assistant write dependency."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from time import monotonic


from . import models
from .command_adapter import ClimateCommandAdapter, Command, CommandResult
from .const import CONF_BEDROOM_CUTOFF_ENABLED, CONF_BEDROOM_CUTOFF_TIME, CONF_BEDROOM_MODE_ENABLED, CONF_BEDROOM_QUIET_ENABLED, CONF_BEDROOM_QUIET_TIME, CONF_BEDROOM_START_TIME, CONF_BEDROOM_TARGET_TEMPERATURE, CONF_CHILD_BEDROOM_START_TIME, CONF_CLIMATE_ENTITY_ID, CONF_COMFORT_TEMPERATURE, CONF_COOLING_START_OFFSET_C, CONF_EMS_GRANTED_STAGES_ENTITY_ID, CONF_EMS_STALE_AFTER_S, CONF_ENERGY_POLICY, CONF_EXPORT_POWER_ENTITY_ID, CONF_EXPORT_POWER_POSITIVE, CONF_HARD_MAX_TEMPERATURE, CONF_HEAT_PUMP_POWER_ENTITY_ID, CONF_HEAT_PUMP_PRIORITY_ENTITY_ID, CONF_HOT_OUTDOOR_COMFORT_TEMPERATURE, CONF_HOUSE_ZONES, CONF_LIVING_EVENING_COMFORT_TEMPERATURE, CONF_LIVING_EVENING_END_TIME, CONF_LIVING_EVENING_START_TIME, CONF_LIVING_ROOM_PILOT_ENABLED, CONF_MANUAL_OVERRIDE_ENABLED, CONF_MILD_OUTDOOR_COMFORT_TEMPERATURE, CONF_MIN_PV_SURPLUS_W, CONF_NO_PV_HOLD_MAX_POWER_W, CONF_OUTDOOR_TEMPERATURE_ENTITY_ID, CONF_OUTDOOR_UNIT_POWER_ENTITY_ID, CONF_PV_FORECAST_POWER_ENTITY_ID, CONF_PV_POWER_ENTITY_ID, CONF_SHADOW_MODE, CONF_SOLAR_IRRADIANCE_ENTITY_ID, CONF_SUN_ENTITY_ID, CONF_TEMPERATURE_ENTITY_ID, CONF_V2_COOLING_SEASON_ENTITY_ID, CONF_V2_HOUSE_CONTROL_ENABLED, CONF_V2_SHADOW_ENABLED, CONF_V2_VACATION_ENTITY_ID, CONF_ZONE_NAME, ControllerState, EnergyPolicy, CONF_OUTDOOR_NO_ACTIVE_COOLING_C, CONF_OUTDOOR_PV_BOOST_EXTRA_W, CONF_OUTDOOR_RAIN_HOLD_PROBABILITY_PCT, CONF_OUTDOOR_RELAXATION_BAND_C, CONF_WEATHER_FORECAST_ENTITY_ID
from .ems_adapter import parse_grant, requested_stages
from .evaluator import evaluate_zone
from .outdoor_cooling_snapshot import build_outdoor_cooling_inputs
from .forecasting import predicted_temperature_60m, temperature_trend_c_per_h
from .house import HousePlan, ZoneTelemetry, build_house_plan
from .house_learning import HouseLearningModel
from .models import ControllerConfig, EMSGrant, EnergySnapshot, ThermalProfile, ThermalResponse, ZoneConfig, ZoneDecision, ZoneForecast, ZoneInput
from .outdoor_unit import HISENSE_5AMW125U4RTA
from .power_learning import OutdoorPowerLearner, PowerEstimate
from .thermal_budget import build_thermal_budget
from .thermal_response import learn_thermal_response
from .thermal_analysis import learn_thermal_profile
from .v2_models import CandidateAction, HouseDecision, RoomCandidate, V2CommandPlan, V2RoomInput
from .v2_shadow import V2ShadowRunner
from .v2_authority import AuthorityDecision, ControlAuthority, HandoffReadiness, RoomAuthorityRegistry
from .v2_command_planner import V2CommandPlanner


def _optional_entity(options: Mapping[str, object], data: Mapping[str, object], key: str) -> str | None:
    """Accept only explicitly selected source entities."""
    value = options.get(key, data.get(key))
    return value if isinstance(value, str) else None


def _zone_number(value: object, default: float) -> float:
    """Uebernimmt einen gespeicherten Zahlenwert, sonst den Integrations-Default.

    0.16.0: Defaults sind Hausregeln (z. B. Schlafraum-Ziel 23,0 C) und gelten
    nur fuer noch nicht gesetzte Felder - ein vom Nutzer gesetzter Wert bleibt.
    """
    resolved = _zone_optional_number(value, default)
    return float(default if resolved is None else resolved)


def _zone_optional_number(value: object, default: float | None) -> float | None:
    """Zahlenwert aus den Optionen, sonst der uebergebene Default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _house_zones(value: object) -> tuple[ZoneConfig, ...]:
    """Load only complete, explicitly configured zone records (plus defaults)."""
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
        # Alte Schreibweise eines Raumnamens ("Schlafzimmrt") wird auch fuer die
        # Identitaet korrigiert; die Raumnummer bleibt die gespeicherte Kennung,
        # damit V2-Autoritaet und Historie stabil bleiben.
        canonical_name = models.canonical_zone_label(normalized_name)
        room_id = str(item.get("zone_id", climate))
        # The existing Schlafzimmer unit is unreliable. It must never become
        # a productive pilot only because an older configuration is upgraded.
        default_pilot_enabled = canonical_name.casefold() != "schlafzimmer"
        # Hausregel 23.09.2026 (models.SCHLAFRAUM_*): die Schlafraeume bekommen
        # Standardwerte (Vorkuehlen erst ab 25,0 C, Ziel 23,0 C, Geraetesoll
        # nicht unter 23,0 C) - nur wo der Nutzer noch nichts gesetzt hat.
        comfort_value = _zone_number(item.get("comfort_temperature"), models.zone_comfort_default(room_id=room_id, name=canonical_name, climate_entity_id=climate))
        acute_value = (
            float(item["acute_cooling_limit_c"])
            if isinstance(item.get("acute_cooling_limit_c"), (int, float))
            else None
        )
        min_outdoor_value = (
            float(item["min_outdoor_cooling_temperature_c"])
            if isinstance(item.get("min_outdoor_cooling_temperature_c"), (int, float))
            else models.zone_min_outdoor_cooling_default(room_id=room_id, name=canonical_name, climate_entity_id=climate)
        )
        # 0.16.1: Fuer die beiden Schlafraeume hat die Hausregel Vorrang vor
        # einem alten Handwert.  Live standen dort noch 24,0 C aus der Zeit vor
        # der Regel; die Migration setzt Komfort 23,0 C, akute Kuehlgrenze
        # 25,0 C und "Kuehlung erst ab Aussentemperatur" 25,0 C durch.
        house_rule = models.schlafraum_house_rule_values(
            room_id=room_id, name=canonical_name, climate_entity_id=climate
        )
        if house_rule is not None:
            comfort_value = house_rule["comfort_temperature"]
            acute_value = house_rule["acute_cooling_limit_c"]
            min_outdoor_value = house_rule["min_outdoor_cooling_temperature_c"]
        result.append(ZoneConfig(
            zone_id=room_id, name=name, climate_entity_id=climate,
            temperature_entity_id=temperature,
            comfort_temperature=comfort_value,
            hard_max_temperature=float(item.get("hard_max_temperature", 25.5)),
            pilot_min_target_temperature=_zone_optional_number(
                item.get("pilot_min_target_temperature"),
                models.zone_pilot_min_default(room_id=room_id, name=canonical_name, climate_entity_id=climate),
            ),
            pilot_max_target_temperature=float(item["pilot_max_target_temperature"]) if isinstance(item.get("pilot_max_target_temperature"), (int, float)) else None,
            hard_limit_failsafe_offset_c=max(0.0, min(8.0, float(item.get("hard_limit_failsafe_offset_c", 1.0)))),
            cooling_power_entity_id=item.get("cooling_power_entity_id") if isinstance(item.get("cooling_power_entity_id"), str) else None,
            priority=int(item.get("priority", 50)),
            modulation_priority=max(1, int(item.get("modulation_priority", 50))),
            pilot_enabled=bool(item.get("pilot_enabled", default_pilot_enabled)),
            use_climate_temperature_fallback=bool(item.get("use_climate_temperature_fallback", False)),
            acute_cooling_limit_c=acute_value,
            min_outdoor_cooling_temperature_c=min_outdoor_value,
            quiet_fan=bool(item.get("quiet_fan", True)),
            forecast_horizon_minutes=float(item.get("forecast_horizon_minutes", 60.0)),
            hold_level_c=float(item.get("hold_level_c", 0.0)) or _migrated_hold_level(item),
            blend_entity_id=str(item.get("blend_entity_id", "") or ""),
            blend_weight_pct=float(item.get("blend_weight_pct", 40.0)),
            shade_entity_ids=shade_ids,
            facade_azimuths=azimuths,
            facade_shade_entity_ids=facade_shades,
            overhang_cutoff_elevation=float(cutoff) if isinstance(cutoff, (int, float)) else None,
        ))
    return tuple(result)


def _migrated_hold_level(item: Mapping[str, object]) -> float:
    """Read a stored hold setting, accepting the old "K unter Komfort" form.

    Before 0.7.3 the level was stored as a delta (``hold_depth_c``).  Existing
    installations are migrated to the absolute form so nobody's setting is lost.
    """
    raw_depth = item.get("hold_depth_c", 0.0)
    depth = float(raw_depth) if isinstance(raw_depth, (int, float)) else 0.0
    if depth <= 0.0:
        return 0.0
    raw_comfort = item.get("comfort_temperature", 0.0)
    comfort = float(raw_comfort) if isinstance(raw_comfort, (int, float)) else 0.0
    return max(0.0, comfort - depth)

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
        "acute_cooling_limit_c": zone.acute_cooling_limit_c,
        "min_outdoor_cooling_temperature_c": zone.min_outdoor_cooling_temperature_c,
        "quiet_fan": zone.quiet_fan,
        "forecast_horizon_minutes": zone.forecast_horizon_minutes,
        "hold_level_c": zone.hold_level_c,
        "blend_entity_id": zone.blend_entity_id,
        "blend_weight_pct": zone.blend_weight_pct,
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
    # Daily holding quality per room ("is the temperature held better?").
    _hold_quality: dict[str, dict] = field(default_factory=dict)
    # Kombi-Logik: letzte Mischrechnung je Raum (Luft, Zweitquelle, Anteil,
    # Ergebnis, Begruendung) - fuer die Anzeige "was + woher" im Dashboard.
    last_blend_info: dict[str, dict] = field(default_factory=dict)
    last_thermal_profiles: dict[str, ThermalProfile] = field(default_factory=dict)
    last_outdoor_gate_decision: object = None
    last_outdoor_gate_snapshot: object = None
    last_outdoor_gate_evaluated_at: float | None = None
    last_outdoor_gate_source_entity_id: str | None = None
    last_outdoor_forecast_hours: tuple[dict, ...] = ()
    last_outdoor_forecast_fetched_at: float | None = None
    power_learner: OutdoorPowerLearner = field(default_factory=OutdoorPowerLearner)
    last_power_estimates: dict[str, PowerEstimate] = field(default_factory=dict)
    house_learning: HouseLearningModel = field(default_factory=HouseLearningModel)
    v2_shadow_runner: V2ShadowRunner = field(default_factory=V2ShadowRunner)
    v2_command_planner: V2CommandPlanner = field(default_factory=V2CommandPlanner)
    last_v2_candidates: tuple[RoomCandidate, ...] = ()
    last_v2_house_decision: HouseDecision | None = None
    last_v2_room_inputs: tuple[V2RoomInput, ...] = ()
    room_authority: RoomAuthorityRegistry = field(default_factory=RoomAuthorityRegistry)
    _last_v2_command_at: dict[str, float] = field(default_factory=dict)
    _v2_transport_failures: dict[str, int] = field(default_factory=dict)
    last_v2_transport_error: str | None = None
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
        configured_minimum_surplus_w = float(options.get(CONF_MIN_PV_SURPLUS_W, data.get(CONF_MIN_PV_SURPLUS_W, 150.0)))
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
            weather_forecast_entity_id=_optional_entity(options, data, CONF_WEATHER_FORECAST_ENTITY_ID),
            outdoor_relaxation_band_c=float(options.get(CONF_OUTDOOR_RELAXATION_BAND_C, data.get(CONF_OUTDOOR_RELAXATION_BAND_C, 1.5))),
            outdoor_no_active_cooling_c=float(options.get(CONF_OUTDOOR_NO_ACTIVE_COOLING_C, data.get(CONF_OUTDOOR_NO_ACTIVE_COOLING_C, 0.5))),
            outdoor_rain_hold_probability_pct=float(options.get(CONF_OUTDOOR_RAIN_HOLD_PROBABILITY_PCT, data.get(CONF_OUTDOOR_RAIN_HOLD_PROBABILITY_PCT, 60.0))),
            outdoor_pv_boost_extra_w=float(options.get(CONF_OUTDOOR_PV_BOOST_EXTRA_W, data.get(CONF_OUTDOOR_PV_BOOST_EXTRA_W, 2000.0))),
        )
        controller = cls(
            config=config,
            command_adapter=ClimateCommandAdapter(
                shadow_mode=False if config.v2_house_control_enabled else shadow_mode,
                productive_enabled=config.v2_house_control_enabled,
            ),
        )
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
            self.observe_hold_quality(zone, sample.temperature_c, mode)
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

    def observe_hold_quality(
        self,
        zone: ZoneConfig,
        temperature_c: float | None,
        mode: str | None,
        now_s: float | None = None,
    ) -> None:
        """Daily holding quality for a room that is kept on a level.

        Household question 2026-09-20: "how do we make sure the temperature is
        held better?"  This records the objective answer per day - how long the
        room air (the configured AirQ sensor, the value V2 regulates on)
        actually stayed within +/-0.3 K of its level, how often the unit
        started, and the day's spread.  Only rooms with a configured hold depth
        are recorded; nothing here is inferred from the device's own sensor.
        """
        hold_level = float(getattr(zone, "hold_level_c", 0.0) or 0.0)
        if hold_level <= 0.0 or temperature_c is None:
            return
        now_s = monotonic() if now_s is None else now_s
        level = hold_level
        today = datetime.now().date()
        stats = self._hold_quality.get(zone.zone_id)
        if stats is None or stats["date"] != today:
            stats = {
                "date": today,
                "zone_name": zone.name,
                "level_c": level,
                "seconds_total": 0.0,
                "seconds_in_band": 0.0,
                "starts": 0,
                "mode": None,
                "temperature_min_c": None,
                "temperature_max_c": None,
                "last_observed_s": None,
            }
        previous = stats["last_observed_s"]
        elapsed = 0.0 if previous is None else max(0.0, min(300.0, now_s - previous))
        stats["level_c"] = level
        stats["seconds_total"] += elapsed
        if abs(float(temperature_c) - level) <= 0.3:
            stats["seconds_in_band"] += elapsed
        low, high = stats["temperature_min_c"], stats["temperature_max_c"]
        stats["temperature_min_c"] = temperature_c if low is None else min(low, temperature_c)
        stats["temperature_max_c"] = temperature_c if high is None else max(high, temperature_c)
        if mode == "cool" and stats["mode"] != "cool":
            stats["starts"] += 1
        stats["mode"] = mode
        stats["last_observed_s"] = now_s
        self._hold_quality[zone.zone_id] = stats

    def hold_quality(self, zone_id: str) -> dict | None:
        """Today's holding quality for one room (None when no level is set)."""
        return self._hold_quality.get(zone_id)

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

    def sun_is_steady(self, zone_id: str, *, minutes: float = 20.0, min_irradiance_w_m2: float = 200.0) -> bool:
        """True when this room saw continuous solar radiation for ``minutes``.

        Household observation 2026-09-20: "ab 25 Grad wird's meist spuerbar warm,
        aber wenn keine konstante Sonneneinstrahlung kommt, ist es okay".  A
        glazed room with steady sun keeps climbing (+0.5 K/h measured), so the
        cooling trigger may act a little earlier then - and stay relaxed when
        the sun only flickers through clouds.
        """
        samples = self._thermal_context_samples.get(zone_id) or []
        if not samples:
            return False
        now = monotonic()
        window = [sample for sample in samples if sample[0] >= now - minutes * 60]
        readings = [sample[6] for sample in window if sample[6] is not None]
        if len(readings) < 3:
            return False
        return all(value >= min_irradiance_w_m2 for value in readings)

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
            "v2_room_authority": self.room_authority.export_state(),
            "command_adapter": self.command_adapter.export_state(),
        }

    def restore_learning_state(self, state: object) -> None:
        """Restore only bounded numeric samples; malformed data is ignored."""
        if not isinstance(state, dict):
            return
        adapter_state = state.get("command_adapter")
        if isinstance(adapter_state, dict):
            self.command_adapter.restore_state(adapter_state)
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
    @property
    def state(self) -> ControllerState:
        """Return an explicit, fail-safe global state."""
        if self.config.shadow_mode:
            return ControllerState.SHADOW
        if self.config.v2_house_control_enabled:
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
        self.command_adapter.set_operating_mode(shadow_mode=enabled, productive_enabled=self.config.v2_house_control_enabled and not enabled)

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
        for zone in self.config.house_zones:
            self.enable_v2_room_shadow(zone.zone_id)
            pending = self.begin_v2_handoff(zone.zone_id, preconditions_met=True)
            if pending.authority.value != "handoff_pending":
                return False
            active = self.activate_v2_authority(zone.zone_id, observed_state_aligned=True)
            if not active.v2_may_write:
                return False
        # A house-wide V2 takeover is also a persistent shutdown of every V1
        # pilot permission.  Authority already prevents V1 writes, but keeping
        # the old switches logically on after a restart is misleading and makes
        # an accidental future reactivation too easy.
        self.config = replace(
            self.config,
            v2_shadow_enabled=True,
            v2_house_control_enabled=True,
        )
        # This adapter is still the only service-call boundary.  V1 cannot
        # use it while every room is V2-owned, so the productive permission
        # applies solely to explicitly approved V2 plans.
        self.command_adapter.set_operating_mode(shadow_mode=False, productive_enabled=True)
        return True

    def restore_v2_house_authority(self, observable_room_ids: set[str]) -> None:
        """Restore V2 ownership room by room after a restart.

        A cloud climate entity can take longer to restore than the controller.
        It must not make the complete house fall back to an inert V1/V2 gap:
        an unobservable room is held in ``handoff_pending`` (so neither writer
        can touch it), while every observable room resumes V2 immediately.
        """
        if not self.config.v2_house_control_enabled:
            return
        for zone in self.config.house_zones:
            authority = self.v2_authority_for(zone.zone_id)
            if authority.authority is ControlAuthority.V1_ACTIVE:
                self.enable_v2_room_shadow(zone.zone_id)
                authority = self.begin_v2_handoff(zone.zone_id, preconditions_met=True)
            if (
                zone.zone_id in observable_room_ids
                and authority.authority is ControlAuthority.HANDOFF_PENDING
            ):
                self.activate_v2_authority(zone.zone_id, observed_state_aligned=True)
        self.command_adapter.set_operating_mode(shadow_mode=False, productive_enabled=True)

    def deactivate_v2_house_control(self) -> None:
        """V2 remains the sole operational controller after activation."""
        return None

    def v2_authority_for(self, zone_id: str) -> AuthorityDecision:
        """Return the visible authority; default ownership is always V1."""
        return self.room_authority.decision_for(zone_id)

    def v2_handoff_readiness(self, zone_id: str) -> HandoffReadiness:
        """Check every precondition without freezing V1 or issuing a command."""
        blockers: list[str] = []
        # 0.16.0: toleranter Raum-Lookup (models.find_zone) statt roher Kennung.
        zone = models.find_zone(self.config.house_zones, zone_id)
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

    def set_manual_override_enabled(self, enabled: bool) -> None:
        """Choose whether a manual climate change is remembered."""
        self.config = replace(self.config, manual_override_enabled=enabled)

    def release_room_manual_takeover(self, zone_id: str) -> bool:
        """Give one manually held room back to V2/V1 at its next safe step."""
        zone = models.find_zone(self.config.house_zones, zone_id)
        if zone is None:
            return False
        self.command_adapter.clear_manual_override(zone.climate_entity_id)
        # A room returned from manual control is a new V2 ownership session.
        self.v2_shadow_runner.reset_room_wind_down(zone.zone_id)
        return True

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

    def living_evening_comfort_active(self, now: time | None = None) -> bool:
        """Return whether the configured occupied-evening comfort window is active."""
        local_time = now or datetime.now().astimezone().time()
        start = self._schedule_time(self.config.living_evening_start_time, time(20, 30))
        end = self._schedule_time(self.config.living_evening_end_time, time(23, 30))
        if start <= end:
            return start <= local_time < end
        return local_time >= start or local_time < end

    def living_night_block_active(self, now: time | None = None) -> bool:
        """Night quiet time that starts when the Abendkomfort window ends.

        Household decision 2026-09-20: the evening comfort end time ("Abendkomfort
        bis", e.g. 23:00) is at the same time the start of the air conditioner's
        night quiet time - one knob, two jobs.  It ends at 07:00; only the hard
        temperature limit may start a room during it.
        """
        local_time = now or datetime.now().astimezone().time()
        start = self._schedule_time(self.config.living_evening_end_time, time(23, 30))
        end = time(7, 0)
        if start <= end:
            return start <= local_time < end
        return local_time >= start or local_time < end

    @staticmethod
    def _schedule_time(value: str, fallback: time) -> time:
        """Parse persisted HH:MM values defensively."""
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
            return time(hour, minute)
        except (AttributeError, TypeError, ValueError):
            return fallback

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

    def evaluate_outdoor_cooling_gate(self, weather_state, *, room_temperature_c: float | None, pv_forecast_w: float | None, acute_cooling_limit_c: float | None = None) -> object:
        """Evaluate the outdoor cooling gate for the Wohnzimmer pilot.

        Returns a tuple ``(decision, snapshot)`` where ``decision`` is the
        pure :class:`OutdoorGateDecision` and ``snapshot`` carries the field
        provenance for the dashboard.  Both are ``None`` only if the gate
        itself cannot be constructed.
        """
        from time import monotonic as _monotonic
        from .outdoor_cooling_gate import evaluate_outdoor_cooling_gate as _eval

        if weather_state is None or self.config.weather_forecast_entity_id is None:
            return None
        if weather_state.entity_id != self.config.weather_forecast_entity_id:
            # We are not the source of truth; a newer coordinator tick owns it.
            return None
        snapshot = build_outdoor_cooling_inputs(
            weather_state=weather_state,
            room_temperature_c=room_temperature_c,
            comfort_temperature_c=self.config.living_evening_comfort_temperature,
            relaxation_band_c=self.config.outdoor_relaxation_band_c,
            no_active_cooling_c=self.config.outdoor_no_active_cooling_c,
            rain_hold_probability_pct=self.config.outdoor_rain_hold_probability_pct,
            pv_forecast_w=pv_forecast_w,
            pv_boost_extra_w=self.config.outdoor_pv_boost_extra_w,
            forecast_hours=self.last_outdoor_forecast_hours or None,
            acute_cooling_limit_c=acute_cooling_limit_c,
        )
        if snapshot.inputs is None:
            return None
        decision = _eval(snapshot.inputs)
        self.last_outdoor_gate_decision = decision
        self.last_outdoor_gate_evaluated_at = _monotonic()
        self.last_outdoor_gate_source_entity_id = self.config.weather_forecast_entity_id
        return decision, snapshot

    def living_room_outdoor_cooling_gate_status(self) -> dict[str, object] | None:
        """Read-only view of the most recent gate decision for the dashboard."""
        decision = self.last_outdoor_gate_decision
        if decision is None:
            return None
        return {
            "decision": decision.decision,
            "reason_code": decision.reason_code,
            "reason_text": decision.reason_text,
            "relaxation_target_c": decision.relaxation_target_c,
            "today_max_outdoor_c": decision.today_max_outdoor_c,
            "gates": decision.gates,
            "source_entity_id": self.last_outdoor_gate_source_entity_id,
            "forecast_hours_count": len(self.last_outdoor_forecast_hours),
        }

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

    def note_v2_transport_failure(self, zone_id: str) -> None:
        """Record a V2 transport failure and request a one-shot relaxed safe hold."""
        zone = models.find_zone(self.config.house_zones, zone_id)
        self._v2_transport_failures[zone_id] = self._v2_transport_failures.get(zone_id, 0) + 1
        if zone is None:
            self.last_v2_transport_error = f"{zone_id}: V2-Transportfehler"
            return
        relaxed_target = zone.pilot_max_target_temperature
        self.last_v2_transport_error = (
            f"{zone.name}: V2-Transportfehler #{self._v2_transport_failures[zone_id]}"
            + (f"; Safe-Hold bei {relaxed_target:g} °C" if relaxed_target is not None else "; Safe-Hold ohne konfiguriertes Gerätesoll")
        )

    async def async_apply_v2_command(self, plan: V2CommandPlan, executor) -> CommandResult:
        """Use V1's sole adapter and supplied executor after explicit authority.

        This method does not make a service call itself.  It is deliberately
        unavailable in V2 Shadow and during handoff/rollback, so a future V2
        executor cannot become a second writer accidentally.
        """
        zone = models.find_zone(self.config.house_zones, plan.room_id)
        if zone is None and self.config.zone is not None and models.zone_matches_room(self.config.zone, plan.room_id):
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
        device_target = plan.target_temperature_c
        command = Command(
            zone.climate_entity_id,
            action,
            device_target,
            urgent=True,
            fan_mode=plan.fan_mode or ("auto" if plan.action is not CandidateAction.STOP else None),
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
        target = models.find_zone(self.config.house_zones, zone_id)
        if target is None:
            return
        zones = tuple(
            replace(zone, use_climate_temperature_fallback=enabled) if zone is target else zone
            for zone in self.config.house_zones
        )
        self.config = replace(self.config, house_zones=zones)

    def zone_by_room_id(self, room_id: str) -> ZoneConfig | None:
        """Read one room by its identifier (tolerant to old spellings).

        0.16.0: Lesen und Schreiben benutzen dieselbe Aufloesung.  Die
        Raum-Entitaeten lesen genau hierueber - vorher verglich die Entitaet rohe
        Zeichenketten und fand einen historisch anders geschriebenen Raum nicht
        mehr (native_value None, number.set_value wirkungslos).
        """
        return models.find_zone(self.config.house_zones, room_id)

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
        acute_cooling_limit_c: float | None = None,
        min_outdoor_cooling_temperature_c: float | None = None,
        forecast_horizon_minutes: float | None = None,
        hold_level_c: float | None = None,
        blend_entity_id: str | None = None,
        blend_weight_pct: float | None = None,
    ) -> None:
        """Change only explicit planning thresholds for one room, never a climate device."""
        # 0.16.0: Adressierung ueber die Raumkennung statt ueber rohe
        # Zeichenketten.  models.find_zone versteht auch die alte Schreibweise
        # ("Schlafzimmrt"); dadurch kommt ein Schreibvorgang wirklich beim Raum
        # an, statt still verworfen zu werden.
        target = models.find_zone(self.config.house_zones, zone_id)
        if target is None:
            return
        updated: list[ZoneConfig] = []
        for zone in self.config.house_zones:
            if zone is not target:
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
                acute_cooling_limit_c=(
                    zone.acute_cooling_limit_c
                    if acute_cooling_limit_c is None
                    else max(16.0, min(32.0, float(acute_cooling_limit_c)))
                ),
                min_outdoor_cooling_temperature_c=(
                    zone.min_outdoor_cooling_temperature_c
                    if min_outdoor_cooling_temperature_c is None
                    else max(0.0, min(32.0, float(min_outdoor_cooling_temperature_c)))
                ),
                forecast_horizon_minutes=(
                    zone.forecast_horizon_minutes
                    if forecast_horizon_minutes is None
                    else max(30.0, min(180.0, float(forecast_horizon_minutes)))
                ),
                hold_level_c=(
                    zone.hold_level_c
                    if hold_level_c is None
                    else (0.0 if float(hold_level_c) <= 0.0 else max(16.0, min(30.0, float(hold_level_c))))
                ),
                blend_entity_id=(
                    zone.blend_entity_id
                    if blend_entity_id is None
                    else str(blend_entity_id).strip()
                ),
                blend_weight_pct=(
                    zone.blend_weight_pct
                    if blend_weight_pct is None
                    else max(0.0, min(70.0, float(blend_weight_pct)))
                ),
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
