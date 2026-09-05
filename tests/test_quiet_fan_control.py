"""Pure-function tests for the draft-minimising fan stage selector."""

import importlib.util
import pathlib
import sys
import types

PACKAGE = "pv_climate_quiet_fan_test"
ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "pv_climate_controller"


def _load():
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.quiet_fan_control", ROOT / "quiet_fan_control.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fan = _load()


def _decide(gap_c, stable_s, boost=False, hard=False, stop=False, current="low", changed_s=9999.0):
    features = fan.FanFeatures(
        gap_c=gap_c, gap_stable_s=stable_s, boost_active=boost,
        hard_limit_exceeded=hard, action_stop=stop,
    )
    state = fan.FanState(current_stage=current, fan_changed_recently_s=changed_s)
    return fan.evaluate_fan_stage(features, state)


def test_small_gap_stays_quiet():
    d = _decide(gap_c=0.4, stable_s=60)
    assert d.stage == "low" and d.reason_code == "gap_small"


def test_persistent_gap_1_5_steps_up_once():
    d = _decide(gap_c=1.8, stable_s=30 * 60, current="low", changed_s=9999)
    assert d.stage == "middle_low" and d.reason_code == "gap_persistent"


def test_persistent_gap_2_5_goes_medium_after_interval():
    d = _decide(gap_c=2.8, stable_s=30 * 60, current="low", changed_s=9999)
    # only one step per interval even for a huge gap
    assert d.stage == "middle_low" and d.reason_code == "step_once"


def test_gap_big_but_grace_not_met_stays():
    d = _decide(gap_c=2.8, stable_s=5 * 60, current="low")
    assert d.stage == "low"


def test_step_interval_blocks_second_step():
    d = _decide(gap_c=2.8, stable_s=30 * 60, current="low", changed_s=60)
    assert d.stage == "low" and d.reason_code == "step_interval"


def test_step_down_when_gap_closes():
    d = _decide(gap_c=0.8, stable_s=120, current="middle_low", changed_s=120)
    assert d.stage == "low"


def test_boost_never_exceeds_middle_low():
    d = _decide(gap_c=3.0, stable_s=3600, boost=True, current="medium", changed_s=9999)
    assert d.stage == "middle_low" and d.reason_code == "boost_light_air"


def test_boost_keeps_current_quiet_stage():
    d = _decide(gap_c=3.0, stable_s=3600, boost=True, current="middle_low", changed_s=9999)
    assert d.stage == "middle_low" and d.reason_code == "boost_keep"


def test_hard_limit_uses_high():
    d = _decide(gap_c=0.0, stable_s=0, hard=True)
    assert d.stage == "high" and d.reason_code == "hard_limit_capacity"


def test_stop_leaves_fan_alone():
    d = _decide(gap_c=2.0, stable_s=3600, stop=True)
    assert d.stage == "auto" and d.reason_code == "stop_no_fan_cmd"


def test_unsupported_quiet_stage_falls_back():
    features = fan.FanFeatures(gap_c=0.4, gap_stable_s=60, supported_stages=("auto", "high"))
    d = fan.evaluate_fan_stage(features, fan.FanState())
    assert d.stage == "auto"


def test_persistent_gap_then_medium_after_two_intervals():
    # First step (after interval) -> middle_low; once that interval has
    # elapsed and the gap is still >= 2.5 K, allow medium.
    d1 = _decide(gap_c=2.9, stable_s=30 * 60, current="low", changed_s=9999)
    assert d1.stage == "middle_low"
    d2 = _decide(gap_c=2.9, stable_s=40 * 60, current="middle_low", changed_s=9999)
    assert d2.stage == "medium"
