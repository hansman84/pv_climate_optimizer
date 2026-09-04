"""Pure-function regression tests for the outdoor cooling gate."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

PACKAGE = "pv_climate_gate_test"
ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "pv_climate_controller"


def _load():
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.outdoor_cooling_gate", ROOT / "outdoor_cooling_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gate = _load()


def _hour(hour: int, temperature: float, precipitation_probability: float = 0.0) -> dict:
    return {
        "datetime": f"2026-09-04T{hour:02d}:00:00",
        "temperature": temperature,
        "precipitation_probability": precipitation_probability,
    }


def test_holds_when_today_max_outdoor_is_well_below_comfort() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=24.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(
                hours=(_hour(10, 21.0), _hour(12, 22.0), _hour(14, 22.5), _hour(16, 22.0), _hour(18, 21.0)),
            ),
            live=gate.OutdoorLive(temperature_c=22.0, uv_index=2.0, cloud_coverage_pct=80.0),
            pv_forecast_w=0.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision == "hold"
    assert decision.reason_code == "outdoor_today_cool"


def test_relaxes_target_when_outdoor_air_almost_matches_room_temperature() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=24.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(
                hours=(_hour(10, 26.0), _hour(12, 28.0), _hour(14, 30.0), _hour(16, 30.0), _hour(18, 28.0)),
            ),
            live=gate.OutdoorLive(temperature_c=23.0, uv_index=0.7, cloud_coverage_pct=80.0),
            pv_forecast_w=0.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision == "hold"
    assert decision.reason_code == "outdoor_equilibrium"
    assert decision.relaxation_target_c == 22.0


def test_pv_boost_activates_on_warm_day_with_pv_forecast() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=25.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(
                hours=(_hour(10, 28.0), _hour(12, 30.0), _hour(14, 32.0), _hour(16, 32.0), _hour(18, 30.0)),
            ),
            live=gate.OutdoorLive(temperature_c=29.0, uv_index=7.0, cloud_coverage_pct=20.0),
            pv_forecast_w=4500.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision == "pv_boosted"
    assert decision.reason_code == "outdoor_pv_boost"


def test_rain_streak_keeps_comfort_inactive_even_on_warm_day() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=25.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(
                hours=(
                    _hour(10, 30.0, 70.0),
                    _hour(11, 30.0, 80.0),
                    _hour(12, 30.0, 75.0),
                    _hour(13, 30.0, 30.0),
                )
            ),
            live=gate.OutdoorLive(temperature_c=29.0, uv_index=4.0, cloud_coverage_pct=70.0),
            pv_forecast_w=4500.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision == "rain_hold"


def test_warm_day_without_pv_keeps_comfort_active() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=25.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(
                hours=(_hour(10, 28.0), _hour(12, 30.0), _hour(14, 32.0), _hour(16, 32.0), _hour(18, 30.0)),
            ),
            live=gate.OutdoorLive(temperature_c=29.0, uv_index=7.0, cloud_coverage_pct=20.0),
            pv_forecast_w=200.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision == "comfort"


def test_missing_forecast_falls_back_to_outdoor_equilibrium_only() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=24.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(hours=()),
            live=gate.OutdoorLive(temperature_c=23.0, uv_index=0.7, cloud_coverage_pct=80.0),
            pv_forecast_w=0.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision in {"hold", "comfort"}
    assert decision.today_max_outdoor_c is None
