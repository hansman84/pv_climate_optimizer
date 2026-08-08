"""Pure V2 priority-contract tests; V1 remains untouched."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path


PACKAGE = "pv_climate_controller_v2_test"
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "pv_climate_controller"


def _load(module: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{module}", ROOT / f"{module}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


models = _load("v2_models")
orchestrator = _load("v2_orchestrator")
authority = _load("v2_authority")
shadow = _load("v2_shadow")
command_planner = _load("v2_command_planner")


def _candidate(
    room_id: str,
    priority: int,
    *,
    budget_w: float = 300.0,
    comfort_gap_c: float = 0.5,
    safety_override: bool = False,
) -> object:
    return models.RoomCandidate(
        models.RoomPolicy(room_id, room_id.title(), priority),
        models.CandidateAction.ADJUST,
        budget_w,
        comfort_gap_c,
        0.9,
        "forecast_comfort_risk",
        "Prognose begruendet eine sanfte Modulationsstufe.",
        safety_override=safety_override,
        next_review_at="2026-08-05T12:10:00+00:00",
    )


def test_fitting_rooms_share_available_pv_budget_in_priority_order() -> None:
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 1, comfort_gap_c=0.3)
    bedroom = _candidate("bedroom", 2, comfort_gap_c=1.5)

    decision = coordinator.decide((living, bedroom), available_budget_w=1000.0)

    assert decision.approved_room_ids == ("living", "bedroom")
    assert decision.reserved_budget_w == 600.0
    assert all(item.state is models.DecisionState.APPROVED_STEP for item in decision.room_decisions)


def test_hard_safety_override_is_approved_alongside_normal_pv_steps() -> None:
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 1)
    bedroom = _candidate("bedroom", 2, safety_override=True)

    decision = coordinator.decide((living, bedroom), available_budget_w=1000.0)

    assert decision.approved_room_ids == ("bedroom", "living")


def test_smaller_lower_priority_step_uses_capacity_when_larger_room_does_not_fit() -> None:
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 1, budget_w=800.0)
    bedroom = _candidate("bedroom", 2, budget_w=100.0)

    decision = coordinator.decide((living, bedroom), available_budget_w=400.0)

    assert decision.approved_room_ids == ("bedroom",)
    assert decision.room_decisions[0].state is models.DecisionState.COMFORT_RISK_ALERT
    assert decision.room_decisions[1].state is models.DecisionState.APPROVED_STEP
    assert decision.reserved_budget_w == 100.0


def test_same_priority_uses_comfort_gap_first_but_approves_all_fitting_steps() -> None:
    coordinator = orchestrator.HouseCoordinator()
    first = _candidate("office", 2, comfort_gap_c=0.3)
    second = _candidate("pantry", 2, comfort_gap_c=0.8)

    decision = coordinator.decide((first, second), available_budget_w=1000.0)

    assert decision.approved_room_ids == ("pantry", "office")


def test_visible_living_room_priority_translates_to_the_first_v2_budget_slot() -> None:
    """V2's ascending rank must preserve the UI's descending room priority."""
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 101 - 91, budget_w=500.0)
    child = _candidate("child", 101 - 76, budget_w=500.0)
    pantry = _candidate("pantry", 101 - 5, budget_w=500.0)

    decision = coordinator.decide((pantry, child, living), available_budget_w=500.0)

    assert decision.approved_room_ids == ("living",)


def test_multiple_rooms_are_limited_by_the_actual_pv_budget() -> None:
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 1, budget_w=500.0)
    bedroom = _candidate("bedroom", 2, budget_w=300.0)
    pantry = _candidate("pantry", 3, budget_w=200.0)

    decision = coordinator.decide((living, bedroom, pantry), available_budget_w=800.0)

    assert decision.approved_room_ids == ("living", "bedroom")
    assert decision.reserved_budget_w == 800.0
    assert decision.room_decisions[2].reason_code == "room_budget_unavailable"


def test_every_candidate_has_a_visible_decision_when_no_modulation_is_requested() -> None:
    candidate = models.RoomCandidate(
        models.RoomPolicy("living", "Wohnzimmer", 1),
        models.CandidateAction.HOLD,
        0.0,
        0.0,
        0.8,
        "comfort_holding",
        "Komfort wird gehalten.",
    )

    decision = orchestrator.HouseCoordinator().decide((candidate,), available_budget_w=500.0)

    assert decision.room_decisions[0].state is models.DecisionState.NOT_REQUESTED
    assert decision.room_decisions[0].reason_code == "comfort_holding"


def test_snapshot_requires_fresh_critical_inputs_but_not_optional_energy_context() -> None:
    valid = models.InputValue("sensor.room", 24.0, "°C", 10.0, models.InputQuality.VALID, "fresh")
    snapshot = models.InputSnapshot(
        "2026-08-05T12:00:00+00:00",
        valid,
        models.InputValue("climate.room", True, None, 2.0, models.InputQuality.VALID, "available"),
        models.InputValue("sensor.export", None, "W", None, models.InputQuality.MISSING, "not_configured"),
        models.InputValue(None, None, "W", None, models.InputQuality.MISSING, "not_configured"),
        valid,
        models.InputValue(None, False, None, None, models.InputQuality.VALID, "not_active"),
        models.InputValue("switch.auto", True, None, 1.0, models.InputQuality.VALID, "enabled"),
        models.InputValue("input_boolean.vacation", False, None, 1.0, models.InputQuality.VALID, "not_active"),
        models.InputValue("binary_sensor.season", True, None, 1.0, models.InputQuality.VALID, "allowed"),
    )

    assert snapshot.critical_inputs_valid


def test_stale_temperature_fails_snapshot_critical_gate() -> None:
    stale_temperature = models.InputValue("sensor.room", 24.0, "°C", 900.0, models.InputQuality.STALE, "stale")
    valid_bool = models.InputValue("sensor.flag", True, None, 1.0, models.InputQuality.VALID, "fresh")
    snapshot = models.InputSnapshot(
        "2026-08-05T12:00:00+00:00", stale_temperature, valid_bool, valid_bool, valid_bool,
        stale_temperature, valid_bool, valid_bool, valid_bool, valid_bool,
    )

    assert not snapshot.critical_inputs_valid
    assert snapshot.critical_input_issues == (stale_temperature,)
    assert snapshot.critical_input_issues[0].source_entity_id == "sensor.room"


def test_authority_handoff_freezes_both_paths_until_observed_state_is_aligned() -> None:
    registry = authority.RoomAuthorityRegistry()

    shadow = registry.enable_shadow("living")
    pending = registry.begin_handoff("living", preconditions_met=True)
    still_pending = registry.activate_v2("living", observed_state_aligned=False)
    active = registry.activate_v2("living", observed_state_aligned=True)

    assert shadow.v1_may_write and not shadow.v2_may_write
    assert not pending.v1_may_write and not pending.v2_may_write
    assert still_pending.authority is authority.ControlAuthority.HANDOFF_PENDING
    assert active.v2_may_write and not active.v1_may_write


def test_authority_rollback_freezes_both_paths_and_survives_restart() -> None:
    registry = authority.RoomAuthorityRegistry()
    registry.enable_shadow("living")
    registry.begin_handoff("living", preconditions_met=True)
    registry.activate_v2("living", observed_state_aligned=True)
    pending = registry.begin_rollback("living")
    restored = authority.RoomAuthorityRegistry.restore(registry.export_state())
    restored_pending = restored.decision_for("living")
    v1 = restored.complete_rollback("living", observed_state_aligned=True)

    assert not pending.v1_may_write and not pending.v2_may_write
    assert restored_pending.authority is authority.ControlAuthority.ROLLBACK_PENDING
    assert v1.authority is authority.ControlAuthority.V1_ACTIVE
    assert v1.v1_may_write


def test_handoff_cannot_skip_shadow_comparison_or_preconditions() -> None:
    registry = authority.RoomAuthorityRegistry()

    decision = registry.begin_handoff("living", preconditions_met=False)

    assert decision.authority is authority.ControlAuthority.V1_ACTIVE
    assert decision.reason_code == "handoff_preconditions_not_met"


def test_shadow_cancellation_returns_authority_to_v1_without_a_handoff() -> None:
    registry = authority.RoomAuthorityRegistry()

    registry.enable_shadow("living")
    v1 = registry.disable_shadow("living")

    assert v1.authority is authority.ControlAuthority.V1_ACTIVE
    assert v1.v1_may_write
    assert not v1.v2_may_write


def test_explicit_v1_room_exclusion_remains_distinguishable_from_default_authority() -> None:
    registry = authority.RoomAuthorityRegistry()

    assert not registry.has_persisted_state("living")
    registry.enable_shadow("living")
    registry.disable_shadow("living")

    assert registry.has_persisted_state("living")
    assert authority.RoomAuthorityRegistry.restore(registry.export_state()).has_persisted_state("living")


def _shadow_room(*, predicted: float | None = 25.0, confidence: float = 0.8, budget_w: float | None = 400.0) -> object:
    valid_temperature = models.InputValue("sensor.living", 24.2, "°C", 10.0, models.InputQuality.VALID, "fresh")
    valid_flag = models.InputValue("sensor.flag", True, None, 1.0, models.InputQuality.VALID, "allowed")
    usable_export = models.InputValue("sensor.export", 500.0, "W", 1.0, models.InputQuality.VALID, "usable_surplus")
    snapshot = models.InputSnapshot(
        "2026-08-05T12:00:00+00:00", valid_temperature, valid_flag, usable_export, valid_flag,
        valid_temperature, valid_flag, valid_flag, valid_flag, valid_flag,
    )
    return models.V2RoomInput(
        models.RoomPolicy("living", "Wohnzimmer", 1), snapshot,
        models.RoomEstimate("living", 24.2, 0.2, predicted, confidence, -0.7, ("trend",), "forecast_ready"),
        models.EligibilityDecision(True, "eligible", "Automatik ist zulässig."), 23.5, 26.0, budget_w,
    )


def test_shadow_runner_approves_one_explainable_step_without_an_executor() -> None:
    candidates, decision = shadow.V2ShadowRunner().evaluate((_shadow_room(),), available_budget_w=500.0)

    assert candidates[0].action is models.CandidateAction.ADJUST
    assert decision.approved_room_ids == ("living",)
    assert decision.room_decisions[0].state is models.DecisionState.APPROVED_STEP


def test_shadow_runner_fails_closed_without_a_learned_budget() -> None:
    candidates, decision = shadow.V2ShadowRunner().evaluate((_shadow_room(budget_w=None),), available_budget_w=1_000.0)

    assert candidates[0].action is models.CandidateAction.HOLD
    assert candidates[0].reason_code == "budget_estimate_missing"
    assert decision.room_decisions[0].state is models.DecisionState.NOT_REQUESTED


def test_shadow_runner_allows_one_adjustment_for_an_already_running_room() -> None:
    room = _shadow_room(budget_w=0.0)
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=24.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    assert candidates[0].action is models.CandidateAction.ADJUST
    assert decision.approved_room_ids == ("living",)


def test_shadow_runner_stops_promptly_once_the_relaxed_target_is_confirmed() -> None:
    """A room must not linger at 25 °C after wind-down has taken effect."""
    base = _shadow_room(budget_w=0.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    cold_target = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, base.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=21.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )
    clock = [0.0]
    runner = shadow.V2ShadowRunner(clock=lambda: clock[0])

    candidates, decision = runner.evaluate((cold_target,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(cold_target, candidates[0], decision)

    assert candidates[0].reason_code == "pv_wind_down"
    assert plan is not None and plan.target_temperature_c == 25.0

    clock[0] = 2 * 60 + 1
    relaxed_target = models.V2RoomInput(
        cold_target.policy, cold_target.snapshot, cold_target.estimate, cold_target.eligibility,
        cold_target.comfort_temperature_c, cold_target.hard_max_temperature_c, cold_target.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )
    candidates, decision = runner.evaluate((relaxed_target,), available_budget_w=0.0)

    assert candidates[0].reason_code == "pv_surplus_ended"
    assert decision.approved_room_ids == ("living",)


def test_shadow_runner_restarts_wind_down_timer_when_a_room_is_returned_to_v2() -> None:
    """Manual handover must not inherit a stale no-PV stop deadline."""
    base = _shadow_room(budget_w=0.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, base.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )
    clock = [0.0]
    runner = shadow.V2ShadowRunner(clock=lambda: clock[0])
    runner.evaluate((room,), available_budget_w=0.0)
    clock[0] = 2 * 60 + 1

    assert runner.evaluate((room,), available_budget_w=0.0)[0][0].reason_code == "pv_surplus_ended"

    runner.reset_room_wind_down("living")

    assert runner.evaluate((room,), available_budget_w=0.0)[0][0].reason_code == "pv_wind_down_waiting"


def test_shadow_runner_winds_down_without_pv_even_before_power_learning_is_complete() -> None:
    """Unknown demand can block a start, never trap a running unit after sunset."""
    base = _shadow_room(budget_w=None)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, None,
        observed_hvac_mode="cool", observed_target_temperature_c=21.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    assert candidates[0].reason_code == "pv_wind_down"


def test_wind_down_uses_the_safe_default_upper_target_when_a_room_has_no_explicit_maximum() -> None:
    """The Speis fallback makes 22 -> 25 °C relaxation commandable."""
    base = _shadow_room(budget_w=0.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, 0.0,
        observed_hvac_mode="cool", observed_target_temperature_c=22.0,
        pilot_min_target_temperature_c=22.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "pv_wind_down"
    assert plan is not None and plan.target_temperature_c == 25.0
    assert decision.approved_room_ids == ("living",)


def test_shadow_runner_does_not_stop_a_running_bedroom_with_a_real_deadline_risk() -> None:
    base = _shadow_room(predicted=25.0, budget_w=0.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, base.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, deadline_at_risk=True,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    assert candidates[0].reason_code == "sleep_deadline_risk"
    assert candidates[0].action is models.CandidateAction.ADJUST
    assert decision.approved_room_ids == ("living",)


def test_shadow_runner_uses_a_short_evening_wind_down_after_sunset() -> None:
    base = _shadow_room(budget_w=0.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, base.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, solar_irradiance_w_m2=2.0,
    )
    clock = [0.0]
    runner = shadow.V2ShadowRunner(clock=lambda: clock[0])
    runner.evaluate((room,), available_budget_w=0.0)
    clock[0] = 2 * 60 + 1

    candidates, decision = runner.evaluate((room,), available_budget_w=0.0)

    assert candidates[0].reason_code == "pv_surplus_ended"
    assert decision.approved_room_ids == ("living",)


def test_weekly_real_world_export_samples_do_not_keep_a_room_running_on_meter_noise() -> None:
    """Regression from the four representative hourly rows of 2026-07-31..08-07.

    The input table included all room temperatures, PV/DC power, export,
    irradiance and outdoor temperature.  The decision boundary under test is
    deliberate: the 10 W cloud reading is not usable PV against the configured
    100 W reserve, whereas 678 W is.
    """
    samples = (
        # name, PV DC W, export W, irradiance W/m², outdoor °C, wind-down, expected reason
        ("night", 0.0, 0.0, 2.0, 29.0, 2 * 60 + 1, "pv_surplus_ended"),
        ("sunset", 13.0, 0.0, 7.0, 23.3, 2 * 60 + 1, "pv_surplus_ended"),
        ("cloud_noise", 378.0, 10.0, 93.0, 23.6, 30 * 60 + 1, "pv_surplus_ended"),
        ("usable_pv", 2126.0, 678.0, 336.0, 28.5, 30 * 60 + 1, "living_room_comfort_priority"),
    )
    for _name, _pv_dc_w, export_w, irradiance_w_m2, _outdoor_c, elapsed_s, expected in samples:
        base = _shadow_room(budget_w=0.0)
        export = models.InputValue("sensor.export", export_w, "W", 5.0, models.InputQuality.VALID, "weekly_sample")
        snapshot = models.InputSnapshot(
            base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
            export, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
            base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
            base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
        )
        room = models.V2RoomInput(
            base.policy, snapshot, base.estimate, base.eligibility,
            base.comfort_temperature_c, base.hard_max_temperature_c, base.required_budget_w,
            observed_hvac_mode="cool", observed_target_temperature_c=25.0,
            pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
            target_temperature_step_c=1.0, solar_irradiance_w_m2=irradiance_w_m2,
            pv_surplus_threshold_w=100.0,
        )
        clock = [0.0]
        runner = shadow.V2ShadowRunner(clock=lambda: clock[0])
        runner.evaluate((room,), available_budget_w=export_w)
        clock[0] = elapsed_s
        candidates, _decision = runner.evaluate((room,), available_budget_w=export_w)

        assert candidates[0].reason_code == expected


def test_full_weekly_hourly_inputs_stop_every_comfortable_running_room_without_usable_pv() -> None:
    """Replay every aligned room/PV/weather hour captured from Home Assistant.

    This deliberately holds each room in ``cool`` to prove the safety property
    that failed in production: once usable export is absent for a whole hourly
    sample, a comfortable running device must become a STOP candidate.  Rows
    at/above the hard limit are excluded because their fail-safe cooling must
    win over the energy-saving stop rule.
    """
    fixture = json.loads((Path(__file__).parent / "fixtures" / "v2_weekly_hourly_inputs_2026-08-07.json").read_text())
    points = fixture["points"]
    assert len(points) == 167
    rooms = (
        ("living", "living", 24.0),
        ("office", "office", 23.5),
        ("child", "child", 22.0),
        ("bedroom", "bedroom", 22.5),
        ("pantry", "pantry", 23.5),
    )
    clock = [0.0]
    runner = shadow.V2ShadowRunner(clock=lambda: clock[0])
    low_pv_previous_hour = {room_id: False for room_id, _field, _comfort in rooms}

    for index, point in enumerate(points):
        clock[0] = index * 60.0 * 60.0
        export_w = float(point["export_w"])
        irradiance_w_m2 = float(point["irradiance_w_m2"])
        usable_pv = export_w >= 100.0
        for room_id, field, comfort_c in rooms:
            temperature_c = float(point[field])
            valid = models.InputValue("sensor.flag", True, None, 1.0, models.InputQuality.VALID, "weekly_replay")
            snapshot = models.InputSnapshot(
                datetime.fromtimestamp(float(point["ts"]) / 1000.0, UTC).isoformat(),
                models.InputValue(f"sensor.{room_id}", temperature_c, "°C", 1.0, models.InputQuality.VALID, "weekly_replay"),
                valid,
                models.InputValue("sensor.export", export_w, "W", 1.0, models.InputQuality.VALID, "weekly_replay"),
                valid,
                models.InputValue("sensor.outdoor", float(point["outdoor_c"]), "°C", 1.0, models.InputQuality.VALID, "weekly_replay"),
                valid, valid, valid, valid,
            )
            room = models.V2RoomInput(
                models.RoomPolicy(room_id, room_id, 1),
                snapshot,
                models.RoomEstimate(room_id, temperature_c, 0.0, temperature_c, 0.8, 0.0, (), "weekly_replay"),
                models.EligibilityDecision(True, "eligible", "weekly replay"),
                comfort_c, 26.0, 0.0,
                observed_hvac_mode="cool", observed_target_temperature_c=25.0,
                pilot_min_target_temperature_c=20.0, pilot_max_target_temperature_c=25.0,
                target_temperature_step_c=1.0, solar_irradiance_w_m2=irradiance_w_m2,
                pv_surplus_threshold_w=100.0,
            )
            candidates, _decision = runner.evaluate((room,), available_budget_w=export_w)
            candidate = candidates[0]
            if not usable_pv and temperature_c < 26.0:
                expected = "pv_surplus_ended" if low_pv_previous_hour[room_id] else "pv_wind_down_waiting"
                assert candidate.reason_code == expected, (index, room_id, point, candidate)
            elif usable_pv:
                assert candidate.reason_code not in {"pv_wind_down", "pv_wind_down_waiting", "pv_surplus_ended"}
            low_pv_previous_hour[room_id] = not usable_pv


def test_stopped_living_room_does_not_restart_on_forecast_risk_without_usable_pv() -> None:
    """Wohnzimmer priority ranks PV use; it does not defeat evening shut-down."""
    base = _shadow_room(predicted=24.7, budget_w=450.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, 450.0,
        observed_hvac_mode="off", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, solar_irradiance_w_m2=2.0,
        pv_surplus_threshold_w=100.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    assert candidates[0].reason_code == "pv_start_blocked_no_surplus"
    assert decision.approved_room_ids == ()
    assert command_planner.V2CommandPlanner().plan(room, candidates[0], decision) is None


def test_sunny_living_room_holds_comfort_without_export_but_not_full_power() -> None:
    """Wohnzimmer comfort wins on a bright day, at its mild comfort target."""
    base = _shadow_room(predicted=24.6, confidence=0.8, budget_w=450.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        24.0, base.hard_max_temperature_c, 450.0,
        observed_hvac_mode="off", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=20.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, solar_irradiance_w_m2=300.0,
        pv_surplus_threshold_w=100.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "living_room_comfort_priority_no_pv"
    assert candidates[0].required_budget_w == 0.0
    assert decision.approved_room_ids == ("living",)
    assert plan is not None and plan.action is models.CandidateAction.START
    assert plan.target_temperature_c == 24.0


def test_normal_room_needs_stable_surplus_before_a_new_start() -> None:
    """A cloud spike must not start Speis before the priority rooms need it."""
    base = _shadow_room(predicted=24.5, confidence=0.8, budget_w=300.0)
    room = models.V2RoomInput(
        models.RoomPolicy("pantry", "Speis", 96), base.snapshot,
        models.RoomEstimate("pantry", 24.0, 0.2, 24.5, 0.8, -1.0, ("trend",), "forecast_ready"),
        base.eligibility, 23.5, 26.0, 300.0,
        observed_hvac_mode="off", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=22.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, solar_irradiance_w_m2=300.0,
        pv_surplus_threshold_w=100.0,
    )
    clock = [0.0]
    runner = shadow.V2ShadowRunner(clock=lambda: clock[0])

    candidates, decision = runner.evaluate((room,), available_budget_w=500.0)

    assert candidates[0].reason_code == "pv_start_waiting_stable_surplus"
    assert decision.approved_room_ids == ()

    clock[0] = 5 * 60 + 1
    candidates, decision = runner.evaluate((room,), available_budget_w=500.0)

    assert candidates[0].reason_code == "forecast_comfort_risk"
    assert decision.approved_room_ids == ("pantry",)


def test_living_no_pv_priority_is_disabled_during_evening_window() -> None:
    base = _shadow_room(predicted=24.6, confidence=0.8, budget_w=450.0)
    no_pv = models.InputValue("sensor.export", 0.0, "W", 5.0, models.InputQuality.VALID, "zero_export")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        no_pv, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        24.0, base.hard_max_temperature_c, 450.0,
        observed_hvac_mode="off", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=20.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, solar_irradiance_w_m2=300.0,
        evening_window_active=True, pv_surplus_threshold_w=100.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    assert candidates[0].reason_code == "pv_start_blocked_no_surplus"
    assert decision.approved_room_ids == ()


def test_missing_inverter_telemetry_allows_only_sunny_daytime_living_room_fallback() -> None:
    base = _shadow_room(predicted=24.7, confidence=0.8, budget_w=450.0)
    missing_export = models.InputValue("sensor.export", None, "W", None, models.InputQuality.INVALID, "source_unavailable")
    snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        missing_export, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    room = models.V2RoomInput(
        base.policy, snapshot, base.estimate, base.eligibility,
        base.comfort_temperature_c, base.hard_max_temperature_c, 450.0,
        observed_hvac_mode="off", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, solar_irradiance_w_m2=700.0,
        pv_surplus_threshold_w=100.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "living_room_telemetry_fallback"
    assert candidates[0].safety_override
    assert decision.approved_room_ids == ("living",)
    assert plan is not None and plan.action is models.CandidateAction.START
    assert plan.target_temperature_c == 25.0


def test_living_comfort_priority_uses_measured_zero_but_never_evening_window() -> None:
    base = _shadow_room(predicted=24.7, confidence=0.8, budget_w=450.0)
    zero_export = models.InputValue("sensor.export", 0.0, "W", 1.0, models.InputQuality.VALID, "zero_export")
    zero_snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        zero_export, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    missing_export = models.InputValue("sensor.export", None, "W", None, models.InputQuality.INVALID, "source_unavailable")
    missing_snapshot = models.InputSnapshot(
        base.snapshot.observed_at, base.snapshot.room_temperature, base.snapshot.climate_available,
        missing_export, base.snapshot.outdoor_unit_power_w, base.snapshot.outdoor_temperature,
        base.snapshot.heat_pump_priority, base.snapshot.automation_enabled,
        base.snapshot.vacation_active, base.snapshot.cooling_season_allowed,
    )
    common = dict(
        observed_hvac_mode="off", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, solar_irradiance_w_m2=700.0,
        pv_surplus_threshold_w=100.0,
    )
    measured_zero = models.V2RoomInput(base.policy, zero_snapshot, base.estimate, base.eligibility, base.comfort_temperature_c, base.hard_max_temperature_c, 450.0, **common)
    evening = models.V2RoomInput(base.policy, missing_snapshot, base.estimate, base.eligibility, base.comfort_temperature_c, base.hard_max_temperature_c, 450.0, evening_window_active=True, **common)

    zero_candidate, zero_decision = shadow.V2ShadowRunner().evaluate((measured_zero,), available_budget_w=0.0)
    evening_candidate, evening_decision = shadow.V2ShadowRunner().evaluate((evening,), available_budget_w=0.0)

    assert zero_candidate[0].reason_code == "living_room_comfort_priority_no_pv"
    assert zero_decision.approved_room_ids == ("living",)
    assert evening_candidate[0].reason_code == "pv_start_blocked_no_surplus"
    assert evening_decision.approved_room_ids == ()


def test_shadow_runner_stops_a_bedroom_that_runs_before_its_start_time() -> None:
    room = _shadow_room()
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate,
        models.EligibilityDecision(False, "bedroom_schedule_pending", "Schlafzimmer: Vorkühlung beginnt ab 16:00 Uhr."),
        room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=21.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].action is models.CandidateAction.STOP
    assert candidates[0].reason_code == "bedroom_schedule_pending"
    assert candidates[0].safety_override
    assert decision.approved_room_ids == ("living",)
    assert plan is not None and plan.action is models.CandidateAction.STOP


def test_shadow_runner_keeps_a_bedroom_off_before_its_start_time() -> None:
    room = _shadow_room()
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate,
        models.EligibilityDecision(False, "bedroom_schedule_pending", "Schlafzimmer: Vorkühlung beginnt ab 16:00 Uhr."),
        room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="off", observed_target_temperature_c=21.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    assert candidates[0].action is models.CandidateAction.HOLD
    assert candidates[0].reason_code == "bedroom_schedule_pending"
    assert decision.approved_room_ids == ()


def test_shadow_runner_does_not_spend_another_step_below_the_pilot_floor() -> None:
    room = _shadow_room(budget_w=0.0)
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=21.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    assert candidates[0].reason_code == "pilot_target_floor_reached"
    assert decision.approved_room_ids == ()


def test_shadow_runner_starts_an_off_room_at_its_hard_temperature_limit() -> None:
    room = _shadow_room(budget_w=None)
    room = models.V2RoomInput(
        room.policy, room.snapshot,
        models.RoomEstimate("living", 26.0, 0.2, 26.4, 0.5, -2.4, ("trend",), "forecast_ready"),
        room.eligibility, room.comfort_temperature_c, 26.0, room.required_budget_w,
        observed_hvac_mode="off", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=None,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "hard_temperature_limit_failsafe"
    assert decision.approved_room_ids == ("living",)
    assert plan is not None and plan.action is models.CandidateAction.START
    assert plan.target_temperature_c == 25.0


def test_shadow_runner_relaxes_a_running_room_one_step_after_forecast_recovers() -> None:
    room = _shadow_room(budget_w=0.0, predicted=23.0)
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=21.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "forecast_comfort_recovered"
    assert plan is not None and plan.target_temperature_c == 22.0


def test_shadow_runner_stops_only_after_relief_target_and_comfort_reserve() -> None:
    room = _shadow_room(budget_w=0.0, predicted=23.0)
    room = models.V2RoomInput(
        room.policy, room.snapshot,
        models.RoomEstimate("living", 22.5, -0.2, 23.0, 0.5, 0.5, ("trend",), "forecast_ready"),
        room.eligibility, room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=25.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "comfort_stable_at_relief_target"
    assert plan is not None and plan.action is models.CandidateAction.STOP


def test_shadow_runner_never_requests_a_step_from_an_insufficient_forecast() -> None:
    candidates, decision = shadow.V2ShadowRunner().evaluate((_shadow_room(predicted=None),), available_budget_w=1_000.0)

    assert candidates[0].reason_code == "forecast_insufficient"
    assert decision.approved_room_ids == ()


def test_command_planner_starts_with_the_mildest_explicit_pilot_target() -> None:
    room = _shadow_room()
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="off", pilot_min_target_temperature_c=21.0,
        pilot_max_target_temperature_c=24.0, target_temperature_step_c=1.0,
    )
    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert plan is not None
    assert plan.action is models.CandidateAction.START
    assert plan.target_temperature_c == 24.0


def test_evening_comfort_can_start_without_export_at_its_comfort_target() -> None:
    room = _shadow_room()
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, 24.5, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="off", pilot_min_target_temperature_c=21.0,
        pilot_max_target_temperature_c=25.0, target_temperature_step_c=1.0,
        evening_comfort_active=True,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "evening_comfort_required"
    assert candidates[0].safety_override
    assert decision.approved_room_ids == ("living",)
    assert plan is not None and plan.action is models.CandidateAction.START
    assert plan.target_temperature_c == 24.0


def test_evening_deadline_starts_calm_living_room_lead_in_without_export() -> None:
    room = _shadow_room(predicted=25.8, confidence=0.8)
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, 25.0, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="off", pilot_min_target_temperature_c=21.0,
        pilot_max_target_temperature_c=25.0, target_temperature_step_c=1.0,
        evening_deadline_at_risk=True,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "evening_comfort_deadline_risk"
    assert candidates[0].safety_override
    assert decision.approved_room_ids == ("living",)
    assert plan is not None and plan.action is models.CandidateAction.START
    assert plan.target_temperature_c == 24.0


def test_command_planner_adjusts_only_one_confirmed_device_step() -> None:
    room = _shadow_room()
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=24.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=24.0,
        target_temperature_step_c=1.0,
    )
    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert plan is not None
    assert plan.action is models.CandidateAction.ADJUST
    assert plan.target_temperature_c == 23.0


def test_scheduled_sleep_trajectory_relaxes_an_overcooled_room_one_step() -> None:
    room = _shadow_room()
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility, room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=20.0,
        pilot_min_target_temperature_c=20.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, scheduled_target_temperature_c=23.0,
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].reason_code == "scheduled_comfort_trajectory"
    assert plan is not None and plan.target_temperature_c == 21.0


def test_command_planner_refuses_to_guess_missing_pilot_bounds_or_device_step() -> None:
    room = _shadow_room()
    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    assert command_planner.V2CommandPlanner().plan(room, candidates[0], decision) is None


def test_command_planner_keeps_auto_airflow_for_material_comfort_risk() -> None:
    room = _shadow_room(predicted=26.0)
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility,
        room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=24.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, observed_fan_mode="auto",
        supported_fan_modes=("auto", "medium", "high"),
    )
    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert plan is not None
    assert plan.target_temperature_c == 23.0
    assert plan.fan_mode is None


def test_command_planner_restores_auto_airflow_when_relaxing() -> None:
    room = _shadow_room(predicted=23.0)
    room = models.V2RoomInput(
        room.policy, room.snapshot, room.estimate, room.eligibility,
        room.comfort_temperature_c, room.hard_max_temperature_c, room.required_budget_w,
        observed_hvac_mode="cool", observed_target_temperature_c=21.0,
        pilot_min_target_temperature_c=21.0, pilot_max_target_temperature_c=25.0,
        target_temperature_step_c=1.0, observed_fan_mode="high",
        supported_fan_modes=("auto", "low", "medium", "high"),
    )
    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=0.0)

    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert plan is not None
    assert plan.target_temperature_c == 22.0
    assert plan.fan_mode == "auto"
