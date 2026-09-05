"""HA-aware snapshot for the outdoor cooling gate.

Translates live weather state, hourly forecast entries, room temperature and
PV forecast into a single, deterministic :class:`OutdoorGateInputs` instance.
The translation is intentionally explicit: every field carries a reason code
so the dashboard sensor can surface why a value is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .outdoor_cooling_gate import OutdoorForecast, OutdoorGateInputs, OutdoorLive


@dataclass(frozen=True, slots=True)
class OutdoorCoolingSnapshot:
    """The result of resolving all sources for one gate evaluation."""

    inputs: OutdoorGateInputs | None
    source_provenance: dict[str, str]
    field_provenance: dict[str, str]


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):  # bools are ints in Python; reject them
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_outdoor_cooling_inputs(
    *,
    weather_state: Any,
    room_temperature_c: float | None,
    comfort_temperature_c: float,
    relaxation_band_c: float,
    no_active_cooling_c: float,
    rain_hold_probability_pct: float,
    pv_forecast_w: float | None,
    pv_boost_extra_w: float,
    forecast_hours: tuple[dict[str, Any], ...] | None = None,
) -> OutdoorCoolingSnapshot:
    """Build the gate inputs from raw HA values, never raising."""

    attributes = getattr(weather_state, "attributes", {}) or {}
    if not isinstance(attributes, dict):
        attributes = {}

    live_temp = _float_or_none(attributes.get("temperature"))
    uv = _float_or_none(attributes.get("uv_index"))
    cloud = _float_or_none(attributes.get("cloud_coverage"))

    forecast_hours_raw: list[dict[str, Any]] = []
    # Prefer the hourly forecast explicitly fetched via ``weather.get_forecasts``
    # (controller cache).  Fall back to a weather entity that publishes
    # ``attributes.forecast`` itself — non-standard, but kept for compatibility.
    raw_forecast: Any = None
    if forecast_hours:
        raw_forecast = forecast_hours
    else:
        attributes_forecast = attributes.get("forecast")
        if isinstance(attributes_forecast, list):
            raw_forecast = attributes_forecast
    if isinstance(raw_forecast, (list, tuple)):
        for entry in raw_forecast[:48]:
            if not isinstance(entry, dict):
                continue
            forecast_hours_raw.append(
                {
                    "datetime": entry.get("datetime"),
                    "temperature": _float_or_none(entry.get("temperature")),
                    "precipitation_probability": _float_or_none(
                        entry.get("precipitation_probability")
                    ),
                }
            )

    source_provenance: dict[str, str] = {}
    field_provenance: dict[str, str] = {}

    if isinstance(weather_state, object) and getattr(weather_state, "entity_id", None):
        source_provenance["weather"] = str(weather_state.entity_id)
    else:
        source_provenance["weather"] = "missing"

    if live_temp is None:
        field_provenance["temperature_c"] = "weather_temperature_missing"
    else:
        field_provenance["temperature_c"] = "weather_temperature_fresh"
    if uv is None:
        field_provenance["uv_index"] = "weather_uv_index_missing"
    else:
        field_provenance["uv_index"] = "weather_uv_index_fresh"
    if cloud is None:
        field_provenance["cloud_coverage_pct"] = "weather_cloud_coverage_missing"
    else:
        field_provenance["cloud_coverage_pct"] = "weather_cloud_coverage_fresh"

    if not forecast_hours_raw:
        field_provenance["forecast"] = "weather_forecast_missing"
    elif forecast_hours:
        field_provenance["forecast"] = f"weather_forecast_service_{len(forecast_hours_raw)}h"
    else:
        field_provenance["forecast"] = f"weather_forecast_attributes_{len(forecast_hours_raw)}h"

    if pv_forecast_w is None:
        field_provenance["pv_forecast_w"] = "pv_forecast_missing"
    else:
        field_provenance["pv_forecast_w"] = "pv_forecast_fresh"

    if room_temperature_c is None:
        field_provenance["room_temperature_c"] = "room_temperature_missing"
    else:
        field_provenance["room_temperature_c"] = "room_temperature_fresh"

    inputs = OutdoorGateInputs(
        room_temperature_c=room_temperature_c,
        comfort_temperature_c=comfort_temperature_c,
        relaxation_band_c=relaxation_band_c,
        no_active_cooling_c=no_active_cooling_c,
        rain_hold_probability_pct=rain_hold_probability_pct,
        forecast=OutdoorForecast(hours=tuple(forecast_hours_raw)),
        live=OutdoorLive(
            temperature_c=live_temp,
            uv_index=uv,
            cloud_coverage_pct=cloud,
        ),
        pv_forecast_w=pv_forecast_w,
        pv_boost_extra_w=pv_boost_extra_w,
    )

    return OutdoorCoolingSnapshot(
        inputs=inputs,
        source_provenance=source_provenance,
        field_provenance=field_provenance,
    )
