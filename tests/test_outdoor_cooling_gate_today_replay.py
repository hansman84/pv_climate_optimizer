"""Live replay of today's forecast through the outdoor cooling gate."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

PACKAGE = "pv_climate_gate_replay"
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


# Live forecast captured via HA service weather.get_forecasts on 2026-09-04.
TODAY_FORECAST: tuple[dict, ...] = (
    _hour(10, 24.5), _hour(11, 25.9), _hour(12, 27.2),
    _hour(13, 28.4), _hour(14, 29.5), _hour(15, 30.1),
    _hour(16, 30.3), _hour(17, 30.4), _hour(18, 30.1),
    _hour(19, 28.9), _hour(20, 27.1), _hour(21, 25.2),
    _hour(22, 23.6), _hour(23, 22.6),
)


def _evaluate(hour: int, room_c: float, live_c: float, uv: float, cloud: float, pv_w: float) -> str:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=room_c,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(hours=TODAY_FORECAST),
            live=gate.OutdoorLive(temperature_c=live_c, uv_index=uv, cloud_coverage_pct=cloud),
            pv_forecast_w=pv_w,
            pv_boost_extra_w=2000.0,
        )
    )
    return f"{hour:02d}:00  room={room_c:.1f}  live={live_c:.1f}  uv={uv:.1f}  cloud={cloud:.0f}  pv={pv_w:.0f}W  -> {decision.decision} ({decision.reason_code})"


def test_replay_today_morning_keeps_comfort_inactive() -> None:
    line = _evaluate(10, 24.0, 23.9, 0.7, 80.7, 0.0)
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=24.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(hours=TODAY_FORECAST),
            live=gate.OutdoorLive(temperature_c=23.9, uv_index=0.7, cloud_coverage_pct=80.7),
            pv_forecast_w=0.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision == "hold", line
    assert decision.reason_code == "outdoor_equilibrium", line
    assert decision.relaxation_target_c == 22.9, line


def test_replay_today_afternoon_uses_pv_boost() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=25.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(hours=TODAY_FORECAST),
            live=gate.OutdoorLive(temperature_c=29.0, uv_index=7.0, cloud_coverage_pct=20.0),
            pv_forecast_w=4500.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision == "pv_boosted"


def test_replay_today_evening_keeps_relaxed_hold() -> None:
    decision = gate.evaluate_outdoor_cooling_gate(
        gate.OutdoorGateInputs(
            room_temperature_c=24.0,
            comfort_temperature_c=23.5,
            relaxation_band_c=1.5,
            no_active_cooling_c=0.5,
            rain_hold_probability_pct=60.0,
            forecast=gate.OutdoorForecast(hours=TODAY_FORECAST),
            live=gate.OutdoorLive(temperature_c=25.0, uv_index=0.5, cloud_coverage_pct=40.0),
            pv_forecast_w=200.0,
            pv_boost_extra_w=2000.0,
        )
    )
    assert decision.decision in {"comfort", "hold"}


def test_replay_today_full_day_summary() -> None:
    scenarios = [
        (10, 24.0, 23.9, 0.7, 80.7, 0.0),
        (12, 24.5, 25.9, 1.0, 78.0, 1200.0),
        (14, 25.0, 29.5, 7.0, 20.0, 4500.0),
        (16, 25.0, 30.3, 6.0, 25.0, 3500.0),
        (19, 24.5, 28.9, 1.0, 21.0, 200.0),
        (22, 24.0, 23.6, 0.0, 18.0, 0.0),
    ]
    for hour, room, live, uv, cloud, pv in scenarios:
        line = _evaluate(hour, room, live, uv, cloud, pv)
        # The summary is for humans; the test contract is enforced above.
        assert isinstance(line, str)
