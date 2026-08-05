"""Pure V2 priority-contract tests; V1 remains untouched."""

from __future__ import annotations

import importlib.util
import sys
import types
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


def test_fixed_room_priority_beats_larger_normal_comfort_gap() -> None:
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 1, comfort_gap_c=0.3)
    bedroom = _candidate("bedroom", 2, comfort_gap_c=1.5)

    decision = coordinator.decide((living, bedroom), available_budget_w=1000.0)

    assert decision.approved_room_ids == ("living",)
    assert decision.room_decisions[1].state is models.DecisionState.WAITING_FOR_OBSERVATION
    assert decision.room_decisions[1].reason_code == "higher_priority_step_active"


def test_hard_safety_override_beats_normal_room_priority() -> None:
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 1)
    bedroom = _candidate("bedroom", 2, safety_override=True)

    decision = coordinator.decide((living, bedroom), available_budget_w=1000.0)

    assert decision.approved_room_ids == ("bedroom",)


def test_lower_priority_room_cannot_consume_budget_reserved_for_priority_room() -> None:
    coordinator = orchestrator.HouseCoordinator()
    living = _candidate("living", 1, budget_w=800.0)
    bedroom = _candidate("bedroom", 2, budget_w=100.0)

    decision = coordinator.decide((living, bedroom), available_budget_w=400.0)

    assert decision.approved_room_ids == ()
    assert decision.room_decisions[0].state is models.DecisionState.COMFORT_RISK_ALERT
    assert decision.room_decisions[1].state is models.DecisionState.BLOCKED_WITH_ESCALATION
    assert decision.room_decisions[1].reason_code == "budget_reserved_for_priority_room"


def test_same_priority_uses_comfort_gap_as_only_tiebreaker() -> None:
    coordinator = orchestrator.HouseCoordinator()
    first = _candidate("office", 2, comfort_gap_c=0.3)
    second = _candidate("pantry", 2, comfort_gap_c=0.8)

    decision = coordinator.decide((first, second), available_budget_w=1000.0)

    assert decision.approved_room_ids == ("pantry",)


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


def _shadow_room(*, predicted: float | None = 25.0, confidence: float = 0.8, budget_w: float | None = 400.0) -> object:
    valid_temperature = models.InputValue("sensor.living", 24.2, "°C", 10.0, models.InputQuality.VALID, "fresh")
    valid_flag = models.InputValue("sensor.flag", True, None, 1.0, models.InputQuality.VALID, "allowed")
    snapshot = models.InputSnapshot(
        "2026-08-05T12:00:00+00:00", valid_temperature, valid_flag, valid_flag, valid_flag,
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


def test_command_planner_refuses_to_guess_missing_pilot_bounds_or_device_step() -> None:
    room = _shadow_room()
    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    assert command_planner.V2CommandPlanner().plan(room, candidates[0], decision) is None
