"""Read-only learning of observed passive warming and cooling response."""

from __future__ import annotations

from collections.abc import Sequence

from .forecasting import temperature_trend_c_per_h
from .models import ThermalResponse


def learn_thermal_response(samples: Sequence[tuple[float, float, str]]) -> ThermalResponse:
    """Separate trends; auto is excluded because its thermal intent is ambiguous."""
    passive = [(timestamp, temperature) for timestamp, temperature, mode in samples if mode in {"off", "fan_only"}]
    cooling = [(timestamp, temperature) for timestamp, temperature, mode in samples if mode in {"cool", "dry"}]
    passive_trend = temperature_trend_c_per_h(passive)
    cooling_trend = temperature_trend_c_per_h(cooling)
    effect = None if passive_trend is None or cooling_trend is None else round(passive_trend - cooling_trend, 3)
    return ThermalResponse(
        None if passive_trend is None else round(passive_trend, 3),
        None if cooling_trend is None else round(cooling_trend, 3),
        effect, len(passive), len(cooling),
    )
