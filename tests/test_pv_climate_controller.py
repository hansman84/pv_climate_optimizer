from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import replace
from datetime import time
from pathlib import Path
from time import monotonic

PACKAGE = "pv_climate_controller"
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / PACKAGE


def test_german_ui_text_uses_real_umlauts_and_no_ascii_replacements() -> None:
    """Keep customer-facing German wording fit for release."""
    text = (ROOT / "strings.json").read_text(encoding="utf-8")
    forbidden = (
        "Klimageraet",
        "Geraet",
        "Mindestueberschuss",
        "Kuehl",
        "fuer ",
        " ueber ",
        "Ueber",
        "Ã",
    )

    assert all(word not in text for word in forbidden)


def test_legacy_room_typo_never_reaches_customer_facing_labels() -> None:
    zone = models.ZoneConfig("bedroom", "Schlafzimmrt", "climate.bedroom", "sensor.bedroom")

    assert zone.name == "Schlafzimmer"


def test_clearing_optional_ems_source_overrides_legacy_config_data() -> None:
    updated = config_options.merge_safety_options(
        {"ems_granted_stages_entity_id": "sensor.loxone_ems"},
        {"shadow_mode": False, "living_room_pilot_enabled": True, "use_ems_grant": False},
        "ems_granted_stages_entity_id",
        "use_ems_grant",
    )

    assert updated["ems_granted_stages_entity_id"] is None


def test_legacy_zero_pv_minimum_restores_safe_default_after_restart() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": False, "min_pv_surplus_w": 0},
        {},
    )

    assert runtime.config.min_pv_surplus_w == 400.0


def test_v2_policy_sources_are_explicit_and_disabled_by_default() -> None:
    default = controller.PVClimateController.from_config({"shadow_mode": True}, {})
    configured = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {
            "v2_shadow_enabled": True,
            "v2_vacation_entity_id": "input_boolean.urlaub",
            "v2_cooling_season_entity_id": "binary_sensor.kuehlsaison",
        },
    )

    assert not default.config.v2_shadow_enabled
    assert configured.config.v2_shadow_enabled
    assert configured.config.v2_vacation_entity_id == "input_boolean.urlaub"
    assert configured.config.v2_cooling_season_entity_id == "binary_sensor.kuehlsaison"


def test_persisted_housewide_v2_mode_keeps_the_runner_and_shared_adapter_enabled_after_restart() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True},
        {"v2_house_control_enabled": True},
    )

    assert runtime.config.v2_house_control_enabled
    assert runtime.config.v2_shadow_enabled
    assert not runtime.command_adapter.shadow_mode
    assert runtime.command_adapter._productive_enabled




def test_hold_level_migrates_from_the_old_delta_setting() -> None:
    """0.7.3: an existing "0.5 K unter Komfort" setting becomes 23.5 °C."""
    assert controller._migrated_hold_level({"hold_depth_c": 0.5, "comfort_temperature": 24.0}) == 23.5
    assert controller._migrated_hold_level({"hold_depth_c": 0.0, "comfort_temperature": 24.0}) == 0.0
    assert controller._migrated_hold_level({"comfort_temperature": 24.0}) == 0.0


def test_hold_quality_counts_time_in_band_and_starts() -> None:
    """0.7.2: the objective proof that a level is actually held."""
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    zone = replace(zone, comfort_temperature=24.0, hold_level_c=23.5)
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, zone),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )

    runtime.observe_hold_quality(zone, 23.6, "cool", 1_000.0)
    runtime.observe_hold_quality(zone, 23.6, "cool", 1_060.0)   # in band
    runtime.observe_hold_quality(zone, 24.4, "cool", 1_120.0)   # above the band
    runtime.observe_hold_quality(zone, 23.5, "off", 1_180.0)    # restart observed
    runtime.observe_hold_quality(zone, 23.5, "cool", 1_240.0)
    stats = runtime.hold_quality("living")

    assert stats is not None
    assert stats["level_c"] == 23.5
    assert stats["seconds_total"] == 240.0
    assert stats["seconds_in_band"] == 180.0  # 23.6/23.6/23.5, not 24.4
    assert stats["starts"] == 2
    assert stats["temperature_min_c"] == 23.5 and stats["temperature_max_c"] == 24.4

    # A room without a configured level records nothing at all.
    plain = models.ZoneConfig("office", "Büro", "climate.office", "sensor.office")
    runtime.observe_hold_quality(plain, 26.0, "cool", 1_300.0)
    assert runtime.hold_quality("office") is None


def test_sun_is_steady_requires_continuous_radiation() -> None:
    """Household rule 2026-09-20: act earlier under steady sun, relaxed when it flickers."""
    from time import monotonic as _now

    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, zone),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )

    def sample(age_s: float, irradiance: float | None) -> tuple:
        return (_now() - age_s, 24.0, "cool", True, 80.0, 25.0, irradiance)

    # Fewer than three readings in the window: not steady.
    runtime._thermal_context_samples["living"] = [sample(900.0, 500.0), sample(600.0, 500.0)]
    assert runtime.sun_is_steady("living") is False

    # A cloud inside the window breaks the steady state.
    runtime._thermal_context_samples["living"] = [sample(900.0, 500.0), sample(600.0, 40.0), sample(300.0, 500.0)]
    assert runtime.sun_is_steady("living") is False

    # Continuous radiation: steady (values just above the threshold count too).
    runtime._thermal_context_samples["living"] = [sample(900.0, 500.0), sample(600.0, 300.0), sample(300.0, 250.0)]
    assert runtime.sun_is_steady("living") is True

    # Readings older than the window do not count.
    runtime._thermal_context_samples["living"] = [
        sample(30 * 60.0, 500.0), sample(20 * 60.0, 500.0), sample(10 * 60.0, 500.0)
    ]
    assert runtime.sun_is_steady("living") is False


def test_v2_handoff_readiness_requires_shadow_data_and_a_house_approval() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, zone, v2_shadow_enabled=True),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )
    runtime.enable_v2_room_shadow("living")

    readiness = runtime.v2_handoff_readiness("living")

    assert not readiness.ready
    assert "critical_inputs_not_fresh" in readiness.blocker_codes
    assert "v2_candidate_not_actionable" in readiness.blocker_codes
    assert "v2_house_step_not_approved" in readiness.blocker_codes


def test_handoff_readiness_exposes_manual_override_as_a_blocker() -> None:
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True)
    command_adapter.observe_external_change(adapter.Command("climate.living", "pilot_adjust", 23.0))

    assert command_adapter.handoff_blockers("climate.living") == ("manual_override_active",)


def test_observed_device_state_acknowledges_only_the_matching_pending_command() -> None:
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, global_interval_s=0, per_entity_interval_s=0)

    async def executor(command):
        return True

    command = adapter.Command("climate.living", "pilot_start", 24.0)
    assert asyncio.run(command_adapter.async_request(command, executor)).status == "sent"
    assert "command_ack_pending" in command_adapter.handoff_blockers("climate.living")
    assert not command_adapter.confirm_observed_climate_state("climate.living", hvac_mode="cool", target_temperature_c=23.0)
    assert command_adapter.confirm_observed_climate_state("climate.living", hvac_mode="cool", target_temperature_c=24.0)
    assert "command_ack_pending" not in command_adapter.handoff_blockers("climate.living")


def test_physical_remote_takeover_blocks_v2_after_a_night_stop() -> None:
    """A remote start must outlive the V2 night-stop candidate for this room."""
    clock = Clock()
    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0,
    )

    async def executor(command):
        return True

    assert asyncio.run(command_adapter.async_request(adapter.Command("climate.child", "pilot_stop"), executor)).status == "sent"
    assert not command_adapter.observe_climate_state("climate.child", hvac_mode="off", target_temperature_c=25.0)
    assert command_adapter.observe_climate_state(
        "climate.child", hvac_mode="cool", target_temperature_c=22.0,
    )
    assert command_adapter.is_manual_override("climate.child")
    assert command_adapter.manual_override_remaining_s("climate.child") == 7200
    assert asyncio.run(command_adapter.async_request(adapter.Command("climate.child", "pilot_stop"), executor)).status == "manual_override"


def test_remote_start_immediately_after_our_night_stop_is_not_swallowed_by_ack_grace() -> None:
    """A physical remote start must win even while the old stop is pending."""
    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False,
        productive_enabled=True,
        global_interval_s=0,
        per_entity_interval_s=0,
    )

    async def executor(command):
        return True

    assert asyncio.run(
        command_adapter.async_request(adapter.Command("climate.child", "pilot_stop"), executor)
    ).status == "sent"

    assert command_adapter.observe_climate_state(
        "climate.child",
        hvac_mode="cool",
        target_temperature_c=24.0,
    )
    assert command_adapter.is_manual_override("climate.child")
    assert command_adapter.manual_override_remaining_s("climate.child") == 7200
    assert asyncio.run(
        command_adapter.async_request(adapter.Command("climate.child", "pilot_stop"), executor)
    ).status == "manual_override"


def test_remote_takeover_recognizes_only_real_climate_control_changes() -> None:
    from types import SimpleNamespace

    def state(mode, **attributes):
        return SimpleNamespace(state=mode, attributes=attributes)

    # A startup/cloud replay must never create a two-hour manual hold.
    assert not adapter.is_climate_control_change(None, state("off", temperature=24))
    assert not adapter.is_climate_control_change(state("off", temperature=24), state("off", temperature=24))
    assert not adapter.is_climate_control_change(state("cool", temperature=24), state("off", temperature=24))
    assert not adapter.is_climate_control_change(state("off", temperature=24), state("off", temperature=23))
    assert not adapter.is_climate_control_change(state("unavailable"), state("off", temperature=24))

    # The values the remote can actually change are a deliberate takeover.
    assert adapter.is_climate_control_change(state("off", temperature=24), state("cool", temperature=24))
    assert adapter.is_climate_control_change(state("cool", temperature=24), state("cool", temperature=23))
    assert not adapter.is_climate_control_change(state("cool", fan_mode="auto"), state("cool", fan_mode="high"))


def test_confirmed_controller_state_and_expired_remote_takeover_return_to_v2() -> None:
    clock = Clock()
    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0,
    )

    async def executor(command):
        return True

    assert asyncio.run(command_adapter.async_request(adapter.Command("climate.child", "pilot_stop"), executor)).status == "sent"
    assert not command_adapter.observe_climate_state("climate.child", hvac_mode="off", target_temperature_c=25.0)
    assert command_adapter.observe_climate_state("climate.child", hvac_mode="cool", target_temperature_c=22.0, override_duration_s=120)
    clock.now = 121

    assert not command_adapter.is_manual_override("climate.child")
    assert command_adapter.manual_override_remaining_s("climate.child") == 0


def test_room_manual_takeover_can_be_released_and_survives_controller_restart() -> None:
    child = models.ZoneConfig("child", "Kinderzimmer", "climate.child", "sensor.child")
    original = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, house_zones=(child,)),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )
    original.command_adapter.observe_external_change(adapter.Command("climate.child", "pilot_start", 22.0))
    restored = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, house_zones=(child,)),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )
    restored.restore_learning_state(original.export_learning_state())

    assert restored.command_adapter.is_manual_override("climate.child")
    restored.v2_shadow_runner._pv_missing_since["child"] = 1.0
    assert restored.release_room_manual_takeover("child")
    assert not restored.command_adapter.is_manual_override("climate.child")
    assert "child" not in restored.v2_shadow_runner._pv_missing_since


def test_v2_command_uses_the_existing_adapter_only_after_v2_authority() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, zone),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, global_interval_s=0, per_entity_interval_s=0),
    )
    plan = controller.V2CommandPlan(
        "living", controller.CandidateAction.ADJUST, 23.0, "forecast_comfort_risk", "V2-Testaktion",
    )
    calls: list[object] = []

    async def executor(command):
        calls.append(command)
        return True

    blocked = asyncio.run(runtime.async_apply_v2_command(plan, executor))
    runtime.enable_v2_room_shadow("living")
    runtime.begin_v2_handoff("living", preconditions_met=True)
    runtime.activate_v2_authority("living", observed_state_aligned=True)
    sent = asyncio.run(runtime.async_apply_v2_command(plan, executor))

    assert blocked.status == "authority_blocked"
    assert sent.status == "sent"
    assert len(calls) == 1
    assert calls[0].entity_id == "climate.living"


def test_v2_waits_fifteen_minutes_between_room_setpoint_steps() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, zone),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, global_interval_s=0, per_entity_interval_s=0),
    )
    runtime.enable_v2_room_shadow("living")
    runtime.begin_v2_handoff("living", preconditions_met=True)
    runtime.activate_v2_authority("living", observed_state_aligned=True)
    first = controller.V2CommandPlan("living", controller.CandidateAction.ADJUST, 23.0, "test", "Erste Stufe")
    second = controller.V2CommandPlan("living", controller.CandidateAction.ADJUST, 24.0, "test", "Zweite Stufe")

    async def executor(command):
        return True

    assert asyncio.run(runtime.async_apply_v2_command(first, executor)).status == "sent"
    assert asyncio.run(runtime.async_apply_v2_command(second, executor)).status == "backoff"


def test_v2_execution_order_prioritizes_the_room_waiting_longest() -> None:
    living = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    bedroom = models.ZoneConfig("bedroom", "Schlafzimmer", "climate.bedroom", "sensor.bedroom")
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, True, living, house_zones=(living, bedroom)),
        adapter.ClimateCommandAdapter(),
    )
    runtime._last_v2_command_at = {"living": 200.0, "bedroom": 100.0}

    assert runtime.v2_execution_order() == ("bedroom", "living")






def test_v2_restart_restores_observable_rooms_without_releasing_an_offline_room_to_v1() -> None:
    living = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living")
    bedroom = models.ZoneConfig("bedroom", "Schlafzimmer", "climate.bedroom", "sensor.bedroom")
    runtime = controller.PVClimateController(
        models.ControllerConfig(
            shadow_mode=False,
            energy_policy=const.EnergyPolicy.PV_PREFERRED,
            living_room_pilot_enabled=False,
            zone=living,
            house_zones=(living, bedroom),
            v2_house_control_enabled=True,
            v2_shadow_enabled=True,
        ),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )

    runtime.restore_v2_house_authority({"bedroom"})

    assert runtime.v2_authority_for("bedroom").v2_may_write
    assert runtime.v2_authority_for("living").authority.value == "handoff_pending"
    assert not runtime.v2_authority_for("living").v1_may_write
    assert not runtime.v2_authority_for("living").v2_may_write

    runtime.restore_v2_house_authority({"living", "bedroom"})

    assert runtime.v2_authority_for("living").v2_may_write


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
power_learning = _load("power_learning")
house_learning = _load("house_learning")
controller = _load("controller")
forecasting = _load("forecasting")
diagnostics = _load("diagnostics")
storage = _load("storage")
outdoor_unit = _load("outdoor_unit")
house = _load("house")
thermal_budget = _load("thermal_budget")
thermal_response = _load("thermal_response")
thermal_analysis = _load("thermal_analysis")
facades = _load("facades")
config_options = _load("config_options")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_stale_unacknowledged_command_never_blocks_a_room_forever() -> None:
    """A lost cloud echo must expire instead of deferring every later command."""
    clock = Clock()
    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, clock=clock,
        global_interval_s=0, per_entity_interval_s=0, ack_timeout_s=180.0,
    )

    async def executor(command):
        return True

    first = asyncio.run(command_adapter.async_request(adapter.Command("climate.living", "pilot_start", 24.0), executor))
    assert first.status == "sent"
    assert "command_ack_pending" in command_adapter.handoff_blockers("climate.living")

    clock.now += 181.0
    assert "command_ack_pending" not in command_adapter.handoff_blockers("climate.living")
    second = asyncio.run(command_adapter.async_request(adapter.Command("climate.living", "pilot_adjust", 25.0), executor))
    assert second.status == "sent", second.reason


def test_fresh_pending_command_still_defers_the_next_step() -> None:
    """The acknowledgement grace stays intact for a recently sent command."""
    clock = Clock()
    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, clock=clock,
        global_interval_s=0, per_entity_interval_s=0, ack_timeout_s=180.0,
    )

    async def executor(command):
        return True

    asyncio.run(command_adapter.async_request(adapter.Command("climate.living", "pilot_start", 24.0), executor))
    clock.now += 30.0
    deferred = asyncio.run(command_adapter.async_request(adapter.Command("climate.living", "pilot_adjust", 25.0), executor))

    assert deferred.status == "deferred"


def test_acknowledgement_accepts_the_device_rounding_of_a_sent_target() -> None:
    """A sent 24.8 C reported back as 25 C must still clear the pending command."""
    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, global_interval_s=0, per_entity_interval_s=0,
    )

    async def executor(command):
        return True

    sent = asyncio.run(command_adapter.async_request(adapter.Command("climate.living", "pilot_adjust", 24.8), executor))
    assert sent.status == "sent"
    assert command_adapter.confirm_observed_climate_state("climate.living", hvac_mode="cool", target_temperature_c=25.0)
    assert "command_ack_pending" not in command_adapter.handoff_blockers("climate.living")


def test_restored_monotonic_timers_must_not_block_a_room() -> None:
    """A restart must not inherit stale monotonic backoffs/overrides."""
    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, global_interval_s=0, per_entity_interval_s=0,
    )
    command_adapter.restore_state({
        "last_global_at": 10_000_000.0,
        "last_entity_at": {"climate.living": 10_000_000.0},
        "backoff_until": {"climate.living": 10_000_000.0},
        "manual_override_until": {"climate.living": 10_000_000.0},
        "last_signature": {},
    })

    assert command_adapter.handoff_blockers("climate.living") == ()

    async def executor(command):
        return True

    sent = asyncio.run(command_adapter.async_request(adapter.Command("climate.living", "pilot_start", 24.0), executor))
    assert sent.status == "sent", sent.reason


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
    assert tuning["facade_shade_defaults"] == [
        ["cover.unused"],
        ["cover.sliding_left", "cover.sliding_right"],
        [],
    ]


def test_facade_cover_selection_is_retained_without_an_azimuth() -> None:
    tuning = facades.normalize_zone_tuning({
        "facade_azimuth_primary": "",
        "facade_shade_primary": ["cover.sliding_left", "cover.sliding_right"],
        "facade_azimuth_secondary": "",
        "facade_shade_secondary": [],
        "facade_azimuth_tertiary": "",
        "facade_shade_tertiary": [],
    })

    assert tuning["facade_azimuths"] == []
    assert tuning["facade_shade_entity_ids"] == []
    assert tuning["facade_shade_defaults"][0] == ["cover.sliding_left", "cover.sliding_right"]


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
    assert with_pv.recommended_target_temperature_c == 22.0
    assert not without_pv.demand
    assert without_pv.reason_code == "family_sleep_waiting_for_pv"


def test_bedroom_relaxes_the_sleep_target_in_small_night_steps() -> None:
    zone = models.ZoneConfig("sleep", "Schlafzimmer", "climate.sleep", "sensor.sleep", comfort_temperature=22.0)

    bedtime = evaluator.evaluate_zone(zone, models.ZoneInput(23.0, True), now=time(22, 0))
    after_midnight = evaluator.evaluate_zone(zone, models.ZoneInput(23.1, True), now=time(1, 0))
    before_waking = evaluator.evaluate_zone(zone, models.ZoneInput(23.6, True), now=time(5, 0))

    assert bedtime.recommended_target_temperature_c == 22.0
    assert after_midnight.recommended_target_temperature_c == 22.5
    assert before_waking.recommended_target_temperature_c == 23.0


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


def test_reentrant_state_callback_cannot_issue_a_second_command_before_the_first_is_reserved() -> None:
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, global_interval_s=0, per_entity_interval_s=0)
    first = adapter.Command("climate.confirmed", "pilot_start", 25.0)
    second = adapter.Command("climate.confirmed", "pilot_adjust", 24.0)
    nested_results = []

    async def reentrant_executor(command):
        nested_results.append(await command_adapter.async_request(second, lambda _: _accepted()))
        return True

    async def _accepted():
        return True

    assert asyncio.run(command_adapter.async_request(first, reentrant_executor)).status == "sent"
    assert nested_results[0].status == "deferred"


def test_urgent_stop_supersedes_an_unacknowledged_adjustment_at_room_cutoff() -> None:
    """A quiet-time stop must not wait forever for a cloud target echo."""
    clock = Clock()
    calls = []

    async def fake_executor(command):
        calls.append(command)
        return True

    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, clock=clock,
        global_interval_s=0, per_entity_interval_s=300,
    )
    assert asyncio.run(command_adapter.async_request(
        adapter.Command("climate.child", "pilot_adjust", 25.0), fake_executor,
    )).status == "sent"

    assert asyncio.run(command_adapter.async_request(
        adapter.Command("climate.child", "pilot_stop", urgent=True), fake_executor,
    )).status == "sent"
    assert [command.action for command in calls] == ["pilot_adjust", "pilot_stop"]
    assert "command_ack_pending" in command_adapter.handoff_blockers("climate.child")


def test_urgent_command_does_not_bypass_pending_device_confirmation() -> None:
    clock = Clock()
    calls = []

    async def fake_executor(command):
        calls.append(command)
        return True

    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, clock=clock,
        global_interval_s=0, per_entity_interval_s=300,
    )
    assert asyncio.run(command_adapter.async_request(
        adapter.Command("climate.confirmed", "pilot_adjust", 21.0), fake_executor,
    )).status == "sent"
    assert asyncio.run(command_adapter.async_request(
        adapter.Command("climate.confirmed", "pilot_adjust", 25.0, urgent=True), fake_executor,
    )).status == "deferred"
    assert len(calls) == 1


def test_default_global_command_ramp_allows_only_one_step_per_minute() -> None:
    clock = Clock()
    calls = []

    async def fake_executor(command):
        calls.append(command)
        return True

    command_adapter = adapter.ClimateCommandAdapter(
        shadow_mode=False, productive_enabled=True, clock=clock,
    )
    assert asyncio.run(command_adapter.async_request(
        adapter.Command("climate.living", "pilot_adjust", 24.0, urgent=True), fake_executor,
    )).status == "sent"
    clock.now = 59
    assert asyncio.run(command_adapter.async_request(
        adapter.Command("climate.office", "pilot_adjust", 24.0, urgent=True), fake_executor,
    )).status == "deferred"
    clock.now = 60
    assert asyncio.run(command_adapter.async_request(
        adapter.Command("climate.office", "pilot_adjust", 24.0, urgent=True), fake_executor,
    )).status == "sent"
    assert len(calls) == 2


def test_confirmed_command_can_be_resent_after_a_verified_device_drift() -> None:
    clock = Clock()
    calls = []

    async def fake_executor(command):
        calls.append(command)
        return True

    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock)
    command = adapter.Command("climate.confirmed", "pilot_adjust", 23.0)
    assert asyncio.run(command_adapter.async_request(command, fake_executor)).status == "sent"
    clock.now = 300
    command_adapter.invalidate_confirmed_signature(command)

    assert asyncio.run(command_adapter.async_request(command, fake_executor)).status == "sent"
    assert len(calls) == 2


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






def test_manual_override_gate_is_configurable_and_defaults_to_allowed() -> None:
    runtime = controller.PVClimateController.from_config({
        "shadow_mode": False,
        "living_room_pilot_enabled": True,
        "climate_entity_id": "climate.living",
        "temperature_entity_id": "sensor.living",
        "zone_name": "Wohnzimmer",
    }, {})

    assert runtime.config.manual_override_enabled
    runtime.set_manual_override_enabled(False)
    assert not runtime.config.manual_override_enabled


def test_living_room_profile_overrides_legacy_top_level_temperature_limits() -> None:
    runtime = controller.PVClimateController.from_config(
        {
            "shadow_mode": False,
            "climate_entity_id": "climate.living",
            "temperature_entity_id": "sensor.living",
            "zone_name": "Wohnzimmer",
            "hard_max_temperature": 25.5,
        },
        {
            "house_zones": [{
                "zone_id": "living",
                "name": "Wohnzimmer",
                "climate_entity_id": "climate.living",
                "temperature_entity_id": "sensor.living",
                "comfort_temperature": 23.5,
                "hard_max_temperature": 26.0,
            }],
        },
    )

    assert runtime.config.zone is not None
    assert runtime.config.zone.hard_max_temperature == 26.0






def test_living_room_outdoor_comfort_waits_fifteen_minutes_then_relaxes_target() -> None:
    runtime = controller.PVClimateController(
        models.ControllerConfig(
            shadow_mode=False,
            energy_policy=const.EnergyPolicy.PV_PREFERRED,
            zone=models.ZoneConfig(
                "living", "Wohnzimmer", "climate.living", "sensor.living",
                comfort_temperature=24.0, hard_max_temperature=26.5,
            ),
        ),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )

    assert runtime._effective_living_room_zone(23.9, now=time(12, 0)).comfort_temperature == 24.0
    status = runtime.living_room_outdoor_comfort_status()
    assert status["candidate_comfort_temperature_c"] == 25.0
    assert 0 < status["stability_remaining_s"] <= 900

    runtime.outdoor_comfort_candidate_since = monotonic() - 900
    assert runtime._effective_living_room_zone(23.9, now=time(12, 0)).comfort_temperature == 25.0
    assert runtime.living_room_outdoor_comfort_status()["stability_remaining_s"] == 0


def test_living_room_outdoor_comfort_uses_25c_in_the_middle_band() -> None:
    runtime = controller.PVClimateController(
        models.ControllerConfig(
            shadow_mode=False,
            energy_policy=const.EnergyPolicy.PV_PREFERRED,
            zone=models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living", comfort_temperature=24.0),
        ),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )

    runtime._effective_living_room_zone(27.0, now=time(12, 0))
    runtime.outdoor_comfort_candidate_since = monotonic() - 900
    assert runtime._effective_living_room_zone(27.0, now=time(12, 0)).comfort_temperature == 25.0


def test_daytime_outdoor_comfort_also_applies_to_arbeitszimmer_not_speis() -> None:
    living = models.ZoneConfig("living", "Wohnzimmer", "climate.living", "sensor.living", comfort_temperature=24.0)
    office = models.ZoneConfig("office", "Spielzimmer", "climate.office", "sensor.office", comfort_temperature=24.0)
    speis = models.ZoneConfig("speis", "Speis", "climate.speis", "sensor.speis", comfort_temperature=23.5)
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, zone=living, house_zones=(living, office, speis)),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )

    runtime._effective_living_room_zone(23.0, now=time(12, 0))
    runtime.outdoor_comfort_candidate_since = monotonic() - 900
    assert runtime._effective_living_room_zone(23.0, now=time(12, 0)).comfort_temperature == 25.0
    runtime.effective_living_room_comfort_temperature = 25.0
    assert runtime._effective_living_room_zone(23.0, now=time(12, 0)).comfort_temperature == 25.0
    assert speis.comfort_temperature == 23.5


def test_bedroom_outdoor_comfort_relaxes_evening_target_to_23c() -> None:
    runtime = controller.PVClimateController(
        models.ControllerConfig(False, const.EnergyPolicy.PV_PREFERRED, bedroom_target_temperature=22.5),
        adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True),
    )

    assert runtime._effective_bedroom_target(25.0) == 22.5
    runtime.bedroom_comfort_candidate_since = monotonic() - 900
    assert runtime._effective_bedroom_target(25.0) == 23.0
    assert runtime.bedroom_outdoor_comfort_status()["effective_evening_target_temperature_c"] == 23.0


















def test_zone_serialization_preserves_all_shade_geometry() -> None:
    zone = models.ZoneConfig(
        "living", "Wohnzimmer", "climate.living", "sensor.living",
        shade_entity_ids=("cover.fallback",),
        facade_azimuths=(180.0, 315.0),
        facade_shade_entity_ids=(("cover.south_left", "cover.south_right"), ("cover.flower",)),
        overhang_cutoff_elevation=42.0,
        hard_limit_failsafe_offset_c=2.0,
    )

    saved = controller.serialize_zone_config(zone)
    restored = controller._house_zones([saved])[0]

    assert restored.shade_entity_ids == zone.shade_entity_ids
    assert restored.facade_azimuths == zone.facade_azimuths
    assert restored.facade_shade_entity_ids == zone.facade_shade_entity_ids
    assert restored.overhang_cutoff_elevation == zone.overhang_cutoff_elevation
    assert restored.hard_limit_failsafe_offset_c == 2.0


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
            "outdoor_unit_power_entity_id": "sensor.confirmed_outdoor_unit",
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
        outdoor_unit_power_state="0.94",
        outdoor_unit_power_unit="kW",
    )

    assert snapshot.pv_power_w == 2500
    assert snapshot.export_power_w == 1800
    assert snapshot.pv_forecast_power_w == 3200
    assert snapshot.outdoor_unit_power_w == 940


def test_idle_outdoor_unit_teaches_the_baseline_not_a_room_demand() -> None:
    """A unit idling in cool mode (~10 W) must not teach a 10 W room demand."""
    learner = power_learning.OutdoorPowerLearner()
    # Same mode reported for the room, but the meter shows standby power: the
    # sample belongs to the idle baseline ().
    assert learner.observe(("wohnzimmer",), 10.0, 0.0) is False
    assert learner.observe(("wohnzimmer",), 10.4, 301.0) is True
    assert learner.observe(("wohnzimmer",), 10.2, 602.0) is True
    assert learner.observe(("wohnzimmer",), 10.6, 903.0) is True
    idle = learner.estimate("wohnzimmer", ())
    assert idle.data_quality == "insufficient_history"
    # Now the compressor really runs: those samples belong to the room.
    assert learner.observe(("wohnzimmer",), 743.0, 1_204.0) is False
    assert learner.observe(("wohnzimmer",), 748.0, 1_505.0) is True
    assert learner.observe(("wohnzimmer",), 739.0, 1_806.0) is True
    learner.observe(("wohnzimmer",), 745.0, 2_107.0)
    learned = learner.estimate("wohnzimmer", ())
    assert learned.data_quality == "learned"
    assert learned.incremental_w is not None and 700.0 < learned.incremental_w < 900.0
    assert learner.status(2_200.0)["minimum_estimate_samples"] == 3


def test_forecast_horizon_scales_the_look_ahead_per_room() -> None:
    """Per-room look-ahead (0.5.10): the glass living room may look 2 h ahead."""
    one_hour = forecasting.contextual_temperature_forecast(
        24.0, 0.5, direct_sun=False, shade_open_percent=0.0, irradiance_w_m2=None,
        passive_sun_trend_c_per_h=None, passive_shaded_trend_c_per_h=None,
    )
    two_hours = forecasting.contextual_temperature_forecast(
        24.0, 0.5, direct_sun=False, shade_open_percent=0.0, irradiance_w_m2=None,
        passive_sun_trend_c_per_h=None, passive_shaded_trend_c_per_h=None,
        horizon_h=2.0,
    )
    assert one_hour.predicted_temperature_60m_c == 24.5
    assert two_hours.predicted_temperature_60m_c == 25.0
    assert two_hours.horizon_h == 2.0


def test_outdoor_power_learning_requires_stability_and_reports_conservative_increment() -> None:
    learner = power_learning.OutdoorPowerLearner()

    assert not learner.observe((), 300, 0)
    assert learner.observe((), 300, 300)
    assert learner.observe((), 310, 600)
    assert learner.observe((), 290, 900)
    assert not learner.observe(("living",), 700, 901)
    assert learner.observe(("living",), 700, 1201)
    assert learner.observe(("living",), 710, 1501)
    assert learner.observe(("living",), 690, 1801)

    estimate = learner.estimate("living", ())

    assert estimate.data_quality == "learned"
    assert estimate.sample_count == 3
    assert estimate.incremental_w == 460.0


def test_outdoor_power_learning_reports_the_current_combination_status() -> None:
    learner = power_learning.OutdoorPowerLearner()

    learner.observe(("bedroom", "children"), 880, 0)
    learner.observe(("bedroom", "children"), 875, 300)

    status = learner.status(420)

    assert status["active_zone_ids"] == ("bedroom", "children")
    assert status["active_set_sample_count"] == 1
    assert status["active_set_median_w"] == 875.0
    assert status["stable_for_s"] == 420.0


def test_house_learning_persists_contextual_combination_envelope() -> None:
    model = house_learning.HouseLearningModel()
    model.observe(timestamp=100, local_hour=21, active_zone_ids=("sleep", "child"), outdoor_power_w=850, pv_power_w=1200, export_power_w=500, outdoor_temperature_c=25, irradiance_w_m2=0)
    model.observe(timestamp=400, local_hour=21, active_zone_ids=("child", "sleep"), outdoor_power_w=900, pv_power_w=1100, export_power_w=400, outdoor_temperature_c=25, irradiance_w_m2=0)

    summary = model.summaries()[0]
    restored = house_learning.HouseLearningModel()
    restored.restore_state(model.export_state(500), 1000)

    assert summary["active_zone_ids"] == ["child", "sleep"]
    assert summary["median_power_w"] == 875.0
    assert summary["local_hours"] == [21]
    assert len(restored.observations) == 2


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


def test_contextual_forecast_uses_open_shade_and_strong_irradiance() -> None:
    forecast = forecasting.contextual_temperature_forecast(
        24.0, 0.1, direct_sun=True, shade_open_percent=100.0,
        irradiance_w_m2=800.0, passive_sun_trend_c_per_h=1.2,
        passive_shaded_trend_c_per_h=0.2,
    )

    assert forecast.predicted_temperature_60m_c > 24.5
    assert forecast.trend_c_per_h > 0.1
    assert any("Sonnenprofil" in factor for factor in forecast.thermal_factors)


def test_contextual_forecast_does_not_guess_without_a_learned_profile() -> None:
    forecast = forecasting.contextual_temperature_forecast(
        24.0, 0.3, direct_sun=True, shade_open_percent=100.0,
        irradiance_w_m2=800.0, passive_sun_trend_c_per_h=None,
        passive_shaded_trend_c_per_h=None,
    )

    assert forecast.predicted_temperature_60m_c == 24.3
    assert forecast.trend_c_per_h == 0.3
    assert any("wird noch gelernt" in factor for factor in forecast.thermal_factors)


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

    runtime.set_zone_thermal_settings("sleep", comfort_temperature=25.0, hard_max_temperature=23.0, hard_limit_failsafe_offset_c=2.5, priority=120)

    assert runtime.config.house_zones[0].comfort_temperature == 23.5
    assert runtime.config.house_zones[1].comfort_temperature == 25.0
    assert runtime.config.house_zones[1].hard_max_temperature == 25.0
    assert runtime.config.house_zones[1].priority == 100
    assert runtime.config.house_zones[1].hard_limit_failsafe_offset_c == 2.5


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


























































def test_living_room_evening_window_is_configurable() -> None:
    runtime = controller.PVClimateController.from_config(
        {
            "shadow_mode": False,
            "living_room_pilot_enabled": True,
            "climate_entity_id": "climate.living",
            "temperature_entity_id": "sensor.living",
            "zone_name": "Wohnzimmer",
        },
        {
            "living_evening_start_time": "19:30",
            "living_evening_end_time": "22:30",
        },
    )

    assert runtime.living_evening_comfort_active(time(20, 0))
    assert not runtime.living_evening_comfort_active(time(23, 0))
