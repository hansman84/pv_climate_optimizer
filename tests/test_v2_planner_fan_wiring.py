"""Planner wiring tests: V2CommandPlanner must carry quiet fan stages."""

import importlib.util
import pathlib
import sys
import types

PACKAGE = "pv_climate_planner_fan_test"
ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "pv_climate_controller"


def _load(name: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


v2_models = _load("v2_models")
planner_mod = _load("v2_command_planner")
quiet = _load("quiet_fan_control")


def test_targets_are_snapped_onto_the_device_step_grid() -> None:
    """Split units round setpoints; V2 must send the value the device reports."""
    assert planner_mod._snap_target(24.8, 1.0) == 25.0
    assert planner_mod._snap_target(24.8, 0.5) == 25.0
    assert planner_mod._snap_target(23.4, 1.0) == 23.0
    assert planner_mod._snap_target(23.2, 0.5) == 23.0
    assert planner_mod._snap_target(24.25, None) == 24.25


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Room:
    """Minimal V2RoomInput stand-in exposing the fields the planner reads."""

    def __init__(self, room_id, measured, target_step=1.0, comfort=23.5, hard=28.0,
                 observed_fan="auto", supported=None, hvac="cool", observed_target=24.0,
                 lower=16.0, upper=25.0, occupied=False):
        self.policy = v2_models.RoomPolicy(room_id, "Raum", 10)
        self.occupied_window_active = occupied
        self.snapshot = type("S", (), {"room_temperature": type("V", (), {
            "value": measured, "is_valid": True})()})()
        self.estimate = None
        self.eligibility = None
        self.comfort_temperature_c = comfort
        self.hard_max_temperature_c = hard
        self.pilot_min_target_temperature_c = lower
        self.pilot_max_target_temperature_c = upper
        self.target_temperature_step_c = target_step
        self.observed_hvac_mode = hvac
        self.observed_target_temperature_c = observed_target
        self.observed_fan_mode = observed_fan
        self.supported_fan_modes = tuple(supported or ["auto", "low", "middle_low", "medium", "middle_high", "high"])
        self.evening_comfort_active = False
        self.hold_depth_c = None


def _candidate(action="adjust", target_after=22.5, reason="v2_scheduled_cooling_step", gap=1.0):
    return v2_models.RoomCandidate(
        policy=None, action=getattr(v2_models.CandidateAction, action.upper()),
        required_budget_w=0.0, comfort_gap_c=gap, confidence=0.7,
        reason_code=reason, reason_text="test", target_after_c=target_after,
    )


def _house(approved):
    return v2_models.HouseDecision(room_decisions=(), approved_room_ids=tuple(approved),
                                   reserved_budget_w=0.0, available_budget_w=500.0)


def test_setpoint_damping_keeps_a_room_calm_between_changes() -> None:
    """0.7.0 (V1 mechanics): one device target change per room per 10 minutes."""
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("d1", measured=25.0, observed_target=24.0, observed_fan="low")
    cand = _candidate(reason="forecast_comfort_risk", target_after=23.0)

    first = planner.plan(room, cand, _house(["d1"]))
    assert first is not None and first.target_temperature_c == 23.0  # one 1 K step towards 23

    clock.t += 60  # one minute later: still settling
    assert planner.plan(room, cand, _house(["d1"])) is None

    clock.t += 10 * 60  # window elapsed
    assert planner.plan(room, cand, _house(["d1"])) is not None

    # An emergency is never damped.
    clock.t += 60
    acute = _candidate(reason="indoor_acute_need", target_after=23.0)
    assert planner.plan(room, acute, _house(["d1"])) is not None


def test_settle_keeps_a_configured_level_and_only_stops_below_it() -> None:
    """0.7.1: a configured level owns the stop line and is never raised away."""
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    holding = _Room("p1", measured=23.4, comfort=24.0, observed_target=23.5, observed_fan="low")
    holding.hold_depth_c = 0.5
    plan = planner.settle_plan(holding)
    # 23.4 is still above the level's own stop line (23.0): keep holding, never
    # raise the level back to comfort.
    assert plan is None or plan.target_temperature_c == 23.5

    too_cold = _Room("p2", measured=22.9, comfort=24.0, observed_target=23.5, observed_fan="low")
    too_cold.hold_depth_c = 0.5
    stop = planner.settle_plan(too_cold)
    assert stop is not None and stop.action is v2_models.CandidateAction.STOP

    # Without a configured level the old behaviour stands: an unintended
    # precool setpoint is raised back towards comfort.
    plain = _Room("p3", measured=24.2, comfort=24.0, observed_target=23.0, observed_fan="low")
    assert planner.settle_plan(plain) is not None


def test_adjust_carry_low_fan_when_room_in_control():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("r1", measured=23.2, observed_fan="auto")  # gap small vs target 22.5
    plan = planner.plan(room, _candidate(), _house(["r1"]))
    assert plan is not None
    assert plan.fan_mode == "low"


def test_fine_ladder_follows_the_level_with_airflow_instead_of_cycling() -> None:
    """0.7.0: holding a level is an airflow job, not an on/off job."""
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("r2", measured=24.5, observed_fan="low")  # gap ~2.0 vs level 22.5/23.0
    for _ in range(2):
        planner.plan(room, _candidate(), _house(["r2"]))
        clock.t += 15 * 60  # two 15-min ticks => gap confirmed
    plan = planner.plan(room, _candidate(), _house(["r2"]))
    # The room is held on a level below comfort, so the fan is modulated finely
    # (one stage per 0.55 K above the level, one step per 5 min interval).
    assert plan is not None
    assert plan.fan_mode in {"middle_low", "medium", "middle_high"}
    assert plan.fan_mode != "low"


def test_stop_plan_carries_no_fan_command():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("r3", measured=22.0)
    plan = planner.plan(room, _candidate(action="stop", target_after=None, reason="pv_surplus_ended"), _house(["r3"]))
    assert plan is not None and plan.fan_mode is None


def test_settle_stops_when_the_room_is_below_the_comfort_reserve():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    # 0.15.0: Sollwert-Untergrenze = Komfort, Stoppreserve 0,3 K.  Ein Raum bei
    # 23,2 (Komfort 23,5) ist damit schon unter der Stopplinie -> Stopp.
    room = _Room("s1", measured=23.2, comfort=23.5, observed_target=21.0, observed_fan="auto")
    plan = planner.settle_plan(room)
    assert plan is not None
    assert plan.action.value == "stop"


def test_settle_lowers_warm_setpoint_to_reach_comfort():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    # Room 24.7 still above comfort 24, setpoint 25 -> step down to comfort.
    room = _Room("s2", measured=24.7, comfort=24.0, observed_target=25.0, observed_fan="low")
    plan = planner.settle_plan(room)
    assert plan is not None
    assert plan.action.value == "adjust" and plan.target_temperature_c == 24.0
    assert plan.reason_code == "v2_comfort_converge_down"


def test_settle_stops_when_room_below_comfort_reserve():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    # Room 22.9 < comfort 23.5 - 0.6 => comfort reached -> stop, no cold hold.
    room = _Room("s3", measured=22.9, comfort=23.5, observed_target=21.0, observed_fan="auto")
    plan = planner.settle_plan(room)
    assert plan is not None
    assert plan.action.value == "stop"
    assert plan.reason_code == "v2_comfort_reached"


def test_settle_quiets_fan_when_target_already_at_comfort():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("s4", measured=24.2, comfort=24.0, observed_target=24.0, observed_fan="auto")
    plan = planner.settle_plan(room)
    assert plan is not None
    assert plan.action.value == "adjust" and plan.target_temperature_c == 24.0
    assert plan.fan_mode == "low"
    assert plan.reason_code == "v2_fan_fine_step"
    # Once the device reports the quiet stage, no further command is emitted.
    quiet = _Room("s4", measured=24.2, comfort=24.0, observed_target=24.0, observed_fan="low")
    assert planner.settle_plan(quiet) is None


def test_settle_stops_earlier_in_occupied_window():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    # Occupied window (softer 0.4.52 band): room 23.0 vs comfort 23.5 stops.
    room = _Room("o1", measured=23.0, comfort=23.5, observed_target=23.0, occupied=True)
    plan = planner.settle_plan(room)
    assert plan is not None
    assert plan.action.value == "stop"
    assert plan.reason_code == "v2_comfort_reached"


def test_default_stop_reserve_applies_outside_occupied_window():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    # Same numbers, no occupied window: default reserve 0.3 K (0.15.0) -> room
    # 23.4 is still above the stop line 23.2, so no command at all.
    room = _Room("o2", measured=23.4, comfort=23.5, observed_target=23.5, observed_fan="low", occupied=False)
    assert planner.settle_plan(room) is None
