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


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Room:
    """Minimal V2RoomInput stand-in exposing the fields the planner reads."""

    def __init__(self, room_id, measured, target_step=1.0, comfort=23.5, hard=28.0,
                 observed_fan="auto", supported=None, hvac="cool", observed_target=24.0,
                 lower=16.0, upper=25.0):
        self.policy = v2_models.RoomPolicy(room_id, "Raum", 10)
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


def _candidate(action="adjust", target_after=22.5, reason="v2_scheduled_cooling_step", gap=1.0):
    return v2_models.RoomCandidate(
        policy=None, action=getattr(v2_models.CandidateAction, action.upper()),
        required_budget_w=0.0, comfort_gap_c=gap, confidence=0.7,
        reason_code=reason, reason_text="test", target_after_c=target_after,
    )


def _house(approved):
    return v2_models.HouseDecision(room_decisions=(), approved_room_ids=tuple(approved),
                                   reserved_budget_w=0.0, available_budget_w=500.0)


def test_adjust_carry_low_fan_when_room_in_control():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("r1", measured=23.2, observed_fan="auto")  # gap small vs target 22.5
    plan = planner.plan(room, _candidate(), _house(["r1"]))
    assert plan is not None
    assert plan.fan_mode == "low"


def test_persistent_large_gap_steps_up_to_middle_low():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("r2", measured=24.5, observed_fan="auto")  # gap ~2.0 vs target 22.5
    for _ in range(2):
        planner.plan(room, _candidate(), _house(["r2"]))
        clock.t += 15 * 60  # two 15-min ticks => 30 min gap stable
    plan = planner.plan(room, _candidate(), _house(["r2"]))
    assert plan is not None and plan.fan_mode == "middle_low"


def test_stop_plan_carries_no_fan_command():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    room = _Room("r3", measured=22.0)
    plan = planner.plan(room, _candidate(action="stop", target_after=None, reason="pv_surplus_ended"), _house(["r3"]))
    assert plan is not None and plan.fan_mode is None


def test_settle_raises_over_eager_setpoint_towards_comfort():
    clock = _Clock()
    planner = planner_mod.V2CommandPlanner(now_fn=clock)
    # measured 23.2 below comfort 23.5, setpoint 21 -> raise towards comfort.
    room = _Room("s1", measured=23.2, comfort=23.5, observed_target=21.0, observed_fan="auto")
    plan = planner.settle_plan(room)
    assert plan is not None
    assert plan.action.value == "adjust" and plan.target_temperature_c == 22.0
    assert plan.reason_code == "v2_comfort_converge_up"
    assert plan.fan_mode == "low"


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
    assert plan.reason_code == "v2_fan_normalize"
    # Once the device reports the quiet stage, no further command is emitted.
    quiet = _Room("s4", measured=24.2, comfort=24.0, observed_target=24.0, observed_fan="low")
    assert planner.settle_plan(quiet) is None
