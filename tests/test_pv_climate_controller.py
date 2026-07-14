from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import time
from pathlib import Path

PACKAGE = "pv_climate_controller"
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / PACKAGE


def _load(module: str):
    sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE)).__path__ = [str(ROOT)]
    path = ROOT / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{module}", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


const = _load("const")
models = _load("models")
evaluator = _load("evaluator")
ems_adapter = _load("ems_adapter")
adapter = _load("command_adapter")
controller = _load("controller")
pilot = _load("pilot")
forecasting = _load("forecasting")
diagnostics = _load("diagnostics")
storage = _load("storage")
outdoor_unit = _load("outdoor_unit")
house = _load("house")
thermal_budget = _load("thermal_budget")
thermal_response = _load("thermal_response")
thermal_analysis = _load("thermal_analysis")
facades = _load("facades")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_shadow_mode_blocks_every_command_request() -> None:
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=True)

    result = asyncio.run(command_adapter.async_request(adapter.Command("climate.confirmed", "Kühlentscheidung")))

    assert result.status == "shadow"
    assert "blockiert" in result.reason


def test_thermal_learning_counts_shade_only_when_the_facade_is_sunlit() -> None:
    night = thermal_analysis.learn_thermal_profile([
        (0.0, 24.0, "off", False, 0.0, 20.0, 0.0),
        (300.0, 24.1, "off", False, 0.0, 20.0, 0.0),
    ])
    shaded = thermal_analysis.learn_thermal_profile([
        (0.0, 24.0, "off", True, 0.0, 20.0, 500.0),
        (300.0, 24.1, "off", True, 0.0, 20.0, 500.0),
    ])

    assert night.passive_shaded_samples == 0
    assert shaded.passive_shaded_samples == 1


def test_facade_group_keeps_both_sliding_door_rollers_with_its_azimuth() -> None:
    tuning = facades.normalize_zone_tuning({
        "facade_azimuth_primary": "",
        "facade_shade_primary": ["cover.unused"],
        "facade_azimuth_secondary": "180",
        "facade_shade_secondary": ["cover.sliding_left", "cover.sliding_right"],
        "facade_azimuth_tertiary": "",
        "facade_shade_tertiary": [],
    })

    assert tuning["facade_azimuths"] == [180.0]
    assert tuning["facade_shade_entity_ids"] == [["cover.sliding_left", "cover.sliding_right"]]


def test_non_shadow_gate_c_is_still_not_a_write_path() -> None:
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False)

    result = asyncio.run(command_adapter.async_request(adapter.Command("climate.confirmed", "Kühlentscheidung")))

    assert result.status == "blocked"


def test_unavailable_climate_produces_safe_zone_decision() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.confirmed", "sensor.confirmed")

    decision = evaluator.evaluate_zone(zone, models.ZoneInput(temperature_c=26.0, climate_available=False))

    assert decision.state is const.ZoneState.UNAVAILABLE
    assert not decision.requested
    assert decision.reason_code == "climate_unavailable"


def test_controller_never_infers_zone_entities() -> None:
    runtime = controller.PVClimateController.from_config({"shadow_mode": True}, {})

    assert runtime.config.zone is None
    assert runtime.evaluate(models.ZoneInput(temperature_c=26.0, climate_available=True)) is None


def test_hard_temperature_limit_has_priority() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.confirmed", "sensor.confirmed")

    decision = evaluator.evaluate_zone(zone, models.ZoneInput(temperature_c=26.0, climate_available=True))

    assert decision.requested
    assert decision.score >= 100
    assert decision.reason_code == "hard_temperature_limit"


def test_living_room_is_only_comfort_controlled_during_the_day() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")

    decision = evaluator.evaluate_zone(
        zone, models.ZoneInput(24.0, True), now=time(23, 0), pv_surplus_available=True,
    )

    assert not decision.demand
    assert decision.reason_code == "family_living_night_limit"


def test_bedroom_uses_pv_surplus_for_preconditioning_before_sleep() -> None:
    zone = models.ZoneConfig("sleep", "Schlafzimmer", "climate.sleep", "sensor.sleep", comfort_temperature=22.0)

    with_pv = evaluator.evaluate_zone(
        zone, models.ZoneInput(24.0, True), now=time(17, 0), pv_surplus_available=True,
    )
    without_pv = evaluator.evaluate_zone(
        zone, models.ZoneInput(24.0, True), now=time(17, 0), pv_surplus_available=False,
    )

    assert with_pv.demand
    assert with_pv.reason_code == "family_sleep_pv_precondition"
    assert with_pv.recommended_target_temperature_c == 23.0
    assert not without_pv.demand
    assert without_pv.reason_code == "family_sleep_waiting_for_pv"


def test_deduplicates_confirmed_command_and_enforces_global_rate_limit() -> None:
    clock = Clock()
    calls = []

    async def fake_executor(command):
        calls.append(command)
        return True

    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock)
    command = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    assert asyncio.run(command_adapter.async_request(command, fake_executor)).status == "sent"
    assert asyncio.run(command_adapter.async_request(command, fake_executor)).status == "noop"
    other = adapter.Command("climate.other", "set_temperature", 23.0)
    assert asyncio.run(command_adapter.async_request(other, fake_executor)).status == "deferred"
    assert len(calls) == 1


def test_retry_once_then_backoff() -> None:
    clock = Clock()
    calls = []

    async def failing_executor(command):
        calls.append(command)
        return False

    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)
    command = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    result = asyncio.run(command_adapter.async_request(command, failing_executor))
    assert result.status == "failed"
    assert result.attempts == 2
    assert len(calls) == 2
    assert asyncio.run(command_adapter.async_request(command, failing_executor)).status == "backoff"


def test_external_change_sets_override_but_matching_confirmation_does_not() -> None:
    clock = Clock()
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)

    async def fake_executor(command):
        return True

    own = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    asyncio.run(command_adapter.async_request(own, fake_executor))
    assert not command_adapter.observe_external_change(own)
    assert command_adapter.observe_external_change(adapter.Command("climate.confirmed", "set_temperature", 25.0))
    assert command_adapter.is_manual_override("climate.confirmed")


def test_restart_snapshot_preserves_override_and_dedupe() -> None:
    clock = Clock()
    original = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)

    async def fake_executor(command):
        return True

    command = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    asyncio.run(original.async_request(command, fake_executor))
    original.observe_external_change(adapter.Command("climate.confirmed", "set_temperature", 24.0))
    restored = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)
    restored.restore_state(original.export_state())
    assert restored.is_manual_override("climate.confirmed")
    assert asyncio.run(restored.async_request(command, fake_executor)).status == "manual_override"


def test_ems_missing_or_stale_grant_fails_closed() -> None:
    grant = ems_adapter.parse_grant("2", age_s=301, stale_after_s=300)

    assert grant.stages == 0
    assert not grant.available
    assert grant.reason_code == "ems_grant_stale"


def test_ems_valid_grant_is_read_but_does_not_write() -> None:
    runtime = controller.PVClimateController.from_config({"shadow_mode": True}, {})
    grant = runtime.evaluate_ems("2", 1)

    assert grant.stages == 2
    assert grant.available
    assert asyncio.run(runtime.async_apply_last_decision()).status == "shadow"


def test_living_room_pilot_never_selects_a_different_zone() -> None:
    non_living = models.ControllerConfig(
        shadow_mode=False,
        energy_policy=const.EnergyPolicy.PV_PREFERRED,
        zone=models.ZoneConfig("configured_zone", "Speis", "climate.confirmed", "sensor.confirmed"),
    )
    living = models.ControllerConfig(
        shadow_mode=False,
        energy_policy=const.EnergyPolicy.PV_PREFERRED,
        zone=models.ZoneConfig("configured_zone", "Wohnzimmer", "climate.confirmed", "sensor.confirmed"),
    )

    assert pilot.living_room_pilot_eligible(non_living, 1) == (False, "pilot_living_room_only")
    assert pilot.living_room_pilot_eligible(living, 0) == (True, "pilot_eligible")
    assert pilot.living_room_pilot_eligible(living, 1) == (True, "pilot_eligible")


def test_raw_ha_states_are_evaluated_without_a_write() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True, "climate_entity_id": "climate.confirmed", "temperature_entity_id": "sensor.confirmed"},
        {},
    )
    decision = runtime.evaluate_from_states(temperature_state="26.1", climate_state="off")

    assert decision is not None
    assert decision.demand
    assert runtime.last_ems_grant is not None
    assert runtime.last_ems_grant.stages == 0


def test_runtime_controls_update_thresholds_and_notify_diagnostics() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True, "climate_entity_id": "climate.confirmed", "temperature_entity_id": "sensor.confirmed"},
        {},
    )
    notifications = []
    runtime.add_state_listener(lambda: notifications.append("updated"))

    runtime.set_energy_policy(const.EnergyPolicy.COMFORT_FIRST)
    runtime.set_comfort_temperature(25.0)
    runtime.set_hard_max_temperature(24.0)
    runtime.notify_state_listeners()

    assert runtime.config.energy_policy is const.EnergyPolicy.COMFORT_FIRST
    assert runtime.config.zone is not None
    assert runtime.config.zone.comfort_temperature == 25.0
    assert runtime.config.zone.hard_max_temperature == 25.0
    assert notifications == ["updated"]


def test_energy_values_are_normalized_only_for_explicit_sources() -> None:
    runtime = controller.PVClimateController.from_config(
        {
            "shadow_mode": True,
            "pv_power_entity_id": "sensor.confirmed_pv",
            "export_power_entity_id": "sensor.confirmed_export",
            "pv_forecast_power_entity_id": "sensor.confirmed_forecast",
            "export_power_positive": False,
        },
        {},
    )

    snapshot = runtime.evaluate_energy(
        pv_power_state="2.5",
        pv_power_unit="kW",
        export_power_state="-1800",
        export_power_unit="W",
        pv_forecast_power_state="3200",
        pv_forecast_power_unit="W",
    )

    assert snapshot.pv_power_w == 2500
    assert snapshot.export_power_w == 1800
    assert snapshot.pv_forecast_power_w == 3200


def test_energy_values_reject_unknown_units_and_unconfigured_sources() -> None:
    runtime = controller.PVClimateController.from_config({"shadow_mode": True}, {})

    snapshot = runtime.evaluate_energy(pv_power_state="2", pv_power_unit="MW")

    assert snapshot.pv_power_w is None
    assert snapshot.export_power_w is None


def test_options_can_replace_confirmed_input_entities() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True, "climate_entity_id": "climate.old", "temperature_entity_id": "sensor.old"},
        {"climate_entity_id": "climate.confirmed", "temperature_entity_id": "sensor.confirmed"},
    )

    assert runtime.config.zone is not None
    assert runtime.config.zone.climate_entity_id == "climate.confirmed"
    assert runtime.config.zone.temperature_entity_id == "sensor.confirmed"


def test_house_zone_profiles_preserve_explicit_room_entities() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {"house_zones": [{"zone_id": "sleep", "name": "Schlafzimmer", "climate_entity_id": "climate.sleep", "temperature_entity_id": "sensor.sleep", "cooling_power_entity_id": "sensor.sleep_cooling", "priority": 80}]},
    )

    assert len(runtime.config.house_zones) == 1
    assert runtime.config.house_zones[0].cooling_power_entity_id == "sensor.sleep_cooling"


def test_export_sign_setting_changes_only_the_normalized_energy_reading() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True, "export_power_entity_id": "sensor.confirmed_export", "export_power_positive": False},
        {},
    )

    runtime.set_export_power_positive(True)
    snapshot = runtime.evaluate_energy(export_power_state="1300", export_power_unit="W")

    assert snapshot.export_power_w == 1300
    assert runtime.config.export_power_positive


def test_forecast_uses_observed_trend() -> None:
    trend = forecasting.temperature_trend_c_per_h([(0, 23.0), (1800, 23.5), (3600, 24.0)])

    assert trend == 1.0
    assert forecasting.predicted_temperature_60m(24.0, trend) == 25.0


def test_diagnostics_redacts_credentials() -> None:
    result = diagnostics.redact({"token": "abc", "nested": {"api_key": "xyz", "temperature": 24}})

    assert result == {"token": "***", "nested": {"api_key": "***", "temperature": 24}}


def test_storage_rejects_unknown_schema() -> None:
    packed = storage.pack({"manual_override_until": {"climate.confirmed": 10}})

    assert storage.unpack(packed)["manual_override_until"]["climate.confirmed"] == 10
    assert storage.unpack({"version": 99, "runtime": {"unsafe": True}}) == {}


def test_house_plan_uses_verified_outdoor_nominal_capacity() -> None:
    decision = models.ZoneDecision("living", const.ZoneState.REQUESTED, True, 100, True, "demand", "Kühlbedarf")
    plan = house.build_house_plan(
        outdoor_unit.HISENSE_5AMW125U4RTA,
        [house.ZoneTelemetry("living", decision, "cool", 3369)],
    )

    assert round(plan.nominal_budget_btu_h, 3) == 42651.775
    assert plan.observed_cooling_btu_h == 3369
    assert plan.thermal_demand_count == 1


def test_house_plan_blocks_recommendation_for_mixed_heat_and_cool() -> None:
    demand = models.ZoneDecision("living", const.ZoneState.REQUESTED, True, 100, True, "demand", "Kühlbedarf")
    idle = models.ZoneDecision("sleep", const.ZoneState.IDLE, False, 0, False, "idle", "Kein Bedarf")
    plan = house.build_house_plan(
        outdoor_unit.HISENSE_5AMW125U4RTA,
        [house.ZoneTelemetry("living", demand, "cool", 1000), house.ZoneTelemetry("sleep", idle, "heat", None)],
    )

    assert "Heiz- und Kühlmodus" in plan.reason


def test_house_plan_orders_equal_thermal_demand_by_priority() -> None:
    demand = models.ZoneDecision("a", const.ZoneState.REQUESTED, True, 50, True, "demand", "Kühlbedarf")
    plan = house.build_house_plan(
        outdoor_unit.HISENSE_5AMW125U4RTA,
        [house.ZoneTelemetry("living", demand, "off", 0, 50), house.ZoneTelemetry("sleep", demand, "off", 0, 80)],
    )

    assert plan.recommended_zone_ids == ("sleep", "living")


def test_house_plan_exposes_each_room_and_does_not_count_auto_as_cooling() -> None:
    demand = models.ZoneDecision("living", const.ZoneState.REQUESTED, True, 80, True, "demand", "Kühlbedarf")
    plan = house.build_house_plan(
        outdoor_unit.HISENSE_5AMW125U4RTA,
        [house.ZoneTelemetry(
            "living", demand, "auto", 5000, 70,
            name="Wohnzimmer", temperature_c=26.2, climate_available=True,
        )],
    )

    assert plan.active_zone_count == 0
    assert plan.observed_cooling_btu_h == 0
    assert plan.zones[0].name == "Wohnzimmer"
    assert plan.zones[0].temperature_c == 26.2
    assert plan.zones[0].decision.reason_code == "demand"


def test_house_zone_uses_individual_temperature_limits() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {"house_zones": [{
            "zone_id": "sleep", "name": "Schlafzimmer", "climate_entity_id": "climate.sleep",
            "temperature_entity_id": "sensor.sleep", "comfort_temperature": 22.0,
            "hard_max_temperature": 24.0, "priority": 60,
        }]},
    )

    plan = runtime.evaluate_house({"sleep": (models.ZoneInput(24.2, True), "off", None)})

    assert plan.zones[0].decision.reason_code == "hard_temperature_limit"


def test_implausible_indoor_temperature_fails_safe_as_data_quality() -> None:
    zone = models.ZoneConfig("dining", "Speis", "climate.dining", "sensor.dining")

    decision = evaluator.evaluate_zone(zone, models.ZoneInput(temperature_c=0.0, climate_available=True))

    assert decision.state is const.ZoneState.DATA_QUALITY
    assert not decision.demand
    assert decision.reason_code == "temperature_implausible"


def test_zone_forecast_requires_history_and_uses_valid_samples_only() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {"house_zones": [{
            "zone_id": "living", "name": "Wohnzimmer", "climate_entity_id": "climate.living",
            "temperature_entity_id": "sensor.living", "priority": 50,
        }]},
    )

    first = runtime.evaluate_house({"living": (models.ZoneInput(24.0, True), "off", None)})
    now = controller.monotonic()
    runtime._temperature_samples["living"] = [(now - 3600.0, 23.0), (now, 24.0)]
    second = runtime.evaluate_house({"living": (models.ZoneInput(24.0, True), "off", None)})

    assert first.zones[0].forecast is not None
    assert first.zones[0].forecast.data_quality == "insufficient_history"
    assert second.zones[0].forecast is not None
    assert second.zones[0].forecast.trend_c_per_h == 1.0
    assert second.zones[0].forecast.predicted_temperature_60m_c == 25.0


def test_house_plan_explains_pv_policy_without_controlling_devices() -> None:
    demand = models.ZoneDecision("living", const.ZoneState.REQUESTED, True, 50, True, "demand", "Kühlbedarf")

    strict = house.build_house_plan(
        outdoor_unit.HISENSE_5AMW125U4RTA,
        [house.ZoneTelemetry("living", demand, "off", None)],
        energy_policy=const.EnergyPolicy.STRICT_PV,
        export_power_w=200,
        min_pv_surplus_w=1000,
    )
    comfort = house.build_house_plan(
        outdoor_unit.HISENSE_5AMW125U4RTA,
        [house.ZoneTelemetry("living", demand, "off", None)],
        energy_policy=const.EnergyPolicy.COMFORT_FIRST,
        export_power_w=200,
        min_pv_surplus_w=1000,
    )

    assert not strict.energy_permits_cooling
    assert "Strikte PV-Politik" in strict.energy_reason
    assert comfort.energy_permits_cooling
    assert "Komfort priorisiert" in comfort.energy_reason


def test_zone_config_preserves_explicit_climate_temperature_fallback() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {"house_zones": [{
            "zone_id": "dining", "name": "Speis", "climate_entity_id": "climate.dining",
            "temperature_entity_id": "sensor.dining", "use_climate_temperature_fallback": True,
        }]},
    )

    plan = runtime.evaluate_house({
        "dining": (models.ZoneInput(24.0, True, temperature_source="climate_current_temperature"), "off", None),
    })

    assert runtime.config.house_zones[0].use_climate_temperature_fallback
    assert plan.zones[0].temperature_source == "climate_current_temperature"


def test_zone_temperature_fallback_changes_only_the_explicit_zone_setting() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {"house_zones": [
            {"zone_id": "living", "name": "Wohnzimmer", "climate_entity_id": "climate.living", "temperature_entity_id": "sensor.living"},
            {"zone_id": "dining", "name": "Speis", "climate_entity_id": "climate.dining", "temperature_entity_id": "sensor.dining"},
        ]},
    )

    runtime.set_zone_temperature_fallback("dining", True)

    assert not runtime.config.house_zones[0].use_climate_temperature_fallback
    assert runtime.config.house_zones[1].use_climate_temperature_fallback


def test_zone_thermal_settings_update_only_that_zone_and_keep_limits_safe() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {"house_zones": [
            {"zone_id": "living", "name": "Wohnzimmer", "climate_entity_id": "climate.living", "temperature_entity_id": "sensor.living"},
            {"zone_id": "sleep", "name": "Schlafzimmer", "climate_entity_id": "climate.sleep", "temperature_entity_id": "sensor.sleep", "comfort_temperature": 22.0, "hard_max_temperature": 24.0},
        ]},
    )

    runtime.set_zone_thermal_settings("sleep", comfort_temperature=25.0, hard_max_temperature=23.0, priority=120)

    assert runtime.config.house_zones[0].comfort_temperature == 23.5
    assert runtime.config.house_zones[1].comfort_temperature == 25.0
    assert runtime.config.house_zones[1].hard_max_temperature == 25.0
    assert runtime.config.house_zones[1].priority == 100


def test_thermal_budget_calculates_reserve_and_time_to_hard_limit() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    forecast = models.ZoneForecast("living", 1.0, 25.0, 3, "valid")

    budget = thermal_budget.build_thermal_budget(zone, 24.5, forecast)

    assert budget["comfort_reserve_c"] == -1.0
    assert budget["hard_limit_reserve_c"] == 1.0
    assert budget["minutes_to_hard_limit"] == 60.0
    assert budget["priority_bonus"] == 90.0


def test_house_plan_can_prioritize_predicted_breach_before_current_demand() -> None:
    idle = models.ZoneDecision("sleep", const.ZoneState.IDLE, False, 0, False, "idle", "Kein Bedarf")
    plan = house.build_house_plan(
        outdoor_unit.HISENSE_5AMW125U4RTA,
        [house.ZoneTelemetry("sleep", idle, "off", None, thermal_budget={"priority_bonus": 100.0})],
    )

    assert plan.thermal_demand_count == 1
    assert "Prognose priorisiert" in plan.reason


def test_learning_snapshot_preserves_only_recent_temperature_samples() -> None:
    runtime = controller.PVClimateController.from_config({"shadow_mode": True}, {})
    now = controller.monotonic()
    runtime._temperature_samples = {"living": [(now - 120, 24.0), (now - 8000, 23.0)]}

    snapshot = runtime.export_learning_state()
    restored = controller.PVClimateController.from_config({"shadow_mode": True}, {})
    restored.restore_learning_state(snapshot)

    assert len(snapshot["temperature_samples"]["living"]) == 1
    assert len(restored._temperature_samples["living"]) == 1
    assert restored._temperature_samples["living"][0][1] == 24.0


def test_thermal_response_learns_observed_cooling_effect() -> None:
    profile = thermal_response.learn_thermal_response([
        (0.0, 24.0, "off"), (3600.0, 25.0, "off"),
        (3600.0, 25.0, "cool"), (7200.0, 23.0, "cool"),
    ])

    assert profile.passive_trend_c_per_h == 1.0
    assert profile.cooling_trend_c_per_h == -2.0
    assert profile.observed_cooling_effect_c_per_h == 3.0


def test_contextual_thermal_learning_never_bridges_mode_changes() -> None:
    profile = thermal_analysis.learn_thermal_profile([
        (0.0, 24.0, "off", True, 100.0, 24.0),
        (300.0, 24.1, "off", True, 100.0, 24.0),
        (600.0, 24.2, "cool", True, 100.0, 24.0),
        (900.0, 24.0, "cool", True, 100.0, 24.0),
    ])
    assert profile.passive_sun_samples == 1
    assert profile.cooling_samples == 1
    assert profile.passive_sun_trend_c_per_h == 1.2
    assert profile.cooling_trend_c_per_h == -2.4


def test_living_room_pilot_preconditions_from_pv_then_stops_at_target() -> None:
    clock = Clock()
    runtime = models.ControllerConfig(
        shadow_mode=False,
        energy_policy=const.EnergyPolicy.STRICT_PV,
        zone=models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living"),
        living_room_pilot_enabled=True,
        min_pv_surplus_w=1000,
    )
    living_pilot = pilot.LivingRoomPilot(clock)

    assert living_pilot.decide(runtime, temperature_c=23.2, climate_mode="off", granted_stages=1, export_power_w=1200).reason_code == "pilot_demand_stabilizing"
    clock.now = 600
    start = living_pilot.decide(runtime, temperature_c=23.2, climate_mode="off", granted_stages=1, export_power_w=1200)
    assert start.action == "start"
    assert start.target_temperature_c == 23.0

    living_pilot.mark_sent(start)
    clock.now = 900
    adjustment = living_pilot.decide(runtime, temperature_c=23.2, climate_mode="cool", granted_stages=1, export_power_w=2500)
    assert adjustment.action == "adjust"
    assert adjustment.target_temperature_c == 22.5
    living_pilot.mark_sent(adjustment)
    clock.now = 1800
    adjustment = living_pilot.decide(runtime, temperature_c=23.0, climate_mode="cool", granted_stages=1, export_power_w=1200)
    assert adjustment.action == "adjust"
    assert adjustment.target_temperature_c == 23.0
    living_pilot.mark_sent(adjustment)
    clock.now = 2400
    assert living_pilot.decide(runtime, temperature_c=23.0, climate_mode="cool", granted_stages=1, export_power_w=1200).reason_code == "pilot_cooling_active"
    clock.now = 3000
    assert living_pilot.decide(runtime, temperature_c=23.0, climate_mode="cool", granted_stages=1, export_power_w=1200).action == "stop"


def test_living_room_pilot_never_takes_over_external_cooling() -> None:
    runtime = models.ControllerConfig(
        shadow_mode=False,
        energy_policy=const.EnergyPolicy.STRICT_PV,
        zone=models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living"),
        living_room_pilot_enabled=True,
    )

    action = pilot.LivingRoomPilot(lambda: 0).decide(
        runtime, temperature_c=25.0, climate_mode="cool", granted_stages=1, export_power_w=2000,
    )

    assert action.action == "none"
    assert action.reason_code == "external_climate_control"
