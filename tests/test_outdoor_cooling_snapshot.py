"""Pure snapshot-builder tests."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

PACKAGE = "pv_climate_gate_snapshot_test"
ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "pv_climate_controller"


def _load(module: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{module}", ROOT / f"{module}.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


snapshot_module = _load("outdoor_cooling_snapshot")
gate = _load("outdoor_cooling_gate")


class _FakeState:
    def __init__(self, entity_id: str, attributes: dict) -> None:
        self.entity_id = entity_id
        self.attributes = attributes


def test_snapshot_with_forecast_returns_filled_inputs() -> None:
    state = _FakeState(
        "weather.test",
        {
            "temperature": 22.0,
            "uv_index": 6.0,
            "cloud_coverage": 25.0,
            "forecast": [
                {"datetime": "2026-09-04T10:00:00", "temperature": 28.0, "precipitation_probability": 0.0},
                {"datetime": "2026-09-04T11:00:00", "temperature": 30.0, "precipitation_probability": 0.0},
            ],
        },
    )
    snap = snapshot_module.build_outdoor_cooling_inputs(
        weather_state=state,
        room_temperature_c=24.0,
        comfort_temperature_c=23.5,
        relaxation_band_c=1.5,
        no_active_cooling_c=0.5,
        rain_hold_probability_pct=60.0,
        pv_forecast_w=4500.0,
        pv_boost_extra_w=2000.0,
    )
    assert snap.inputs is not None
    assert snap.inputs.live.temperature_c == 22.0
    assert snap.inputs.forecast.hours[0]["temperature"] == 28.0
    assert snap.field_provenance["forecast"] == "weather_forecast_attributes_2h"
    decision = gate.evaluate_outdoor_cooling_gate(snap.inputs)
    assert decision.decision == "pv_boosted"


def test_snapshot_without_weather_state_returns_provenance() -> None:
    snap = snapshot_module.build_outdoor_cooling_inputs(
        weather_state=None,
        room_temperature_c=None,
        comfort_temperature_c=23.5,
        relaxation_band_c=1.5,
        no_active_cooling_c=0.5,
        rain_hold_probability_pct=60.0,
        pv_forecast_w=None,
        pv_boost_extra_w=2000.0,
    )
    assert snap.inputs is not None
    assert snap.field_provenance["temperature_c"] == "weather_temperature_missing"
    assert snap.field_provenance["forecast"] == "weather_forecast_missing"
    decision = gate.evaluate_outdoor_cooling_gate(snap.inputs)
    # Without any source the gate must not invent a value: it falls back to
    # the conservative comfort-active state.
    assert decision.decision in {"comfort", "hold"}


def test_snapshot_prefers_service_forecast_hours_over_attributes() -> None:
    state = _FakeState(
        "weather.test",
        {
            "temperature": 22.0,
            "forecast": [
                {"datetime": "2026-09-04T10:00:00", "temperature": 30.0},
            ],
        },
    )
    snap = snapshot_module.build_outdoor_cooling_inputs(
        weather_state=state,
        room_temperature_c=24.0,
        comfort_temperature_c=23.5,
        relaxation_band_c=1.5,
        no_active_cooling_c=0.5,
        rain_hold_probability_pct=60.0,
        pv_forecast_w=None,
        pv_boost_extra_w=2000.0,
        forecast_hours=(
            {"datetime": "2026-09-04T09:00:00", "temperature": 21.0},
            {"datetime": "2026-09-04T10:00:00", "temperature": 22.0},
        ),
    )
    assert snap.inputs is not None
    # Service-provided hours win over attributes.forecast.
    assert snap.inputs.forecast.hours[0]["temperature"] == 21.0
    assert len(snap.inputs.forecast.hours) == 2
    assert snap.field_provenance["forecast"] == "weather_forecast_service_2h"


def test_service_forecast_hours_enable_mild_day_hold() -> None:
    """A mild today-maximum from the service forecast must hold the room."""
    state = _FakeState(
        "weather.test",
        {
            "temperature": 21.0,
        },
    )
    snap = snapshot_module.build_outdoor_cooling_inputs(
        weather_state=state,
        room_temperature_c=24.0,
        comfort_temperature_c=23.5,
        relaxation_band_c=1.5,
        no_active_cooling_c=0.5,
        rain_hold_probability_pct=60.0,
        pv_forecast_w=3000.0,
        pv_boost_extra_w=2000.0,
        forecast_hours=(
            {"datetime": "2026-09-04T12:00:00", "temperature": 21.0},
            {"datetime": "2026-09-04T13:00:00", "temperature": 22.0},
            {"datetime": "2026-09-04T14:00:00", "temperature": 22.5},
        ),
    )
    assert snap.inputs is not None
    decision = gate.evaluate_outdoor_cooling_gate(snap.inputs)
    assert decision.decision == "hold"
    assert decision.gates["today_cool_enough"] is True
