"""Small, deterministic forecasting helpers."""

from __future__ import annotations

from collections.abc import Sequence


def temperature_trend_c_per_h(samples: Sequence[tuple[float, float]]) -> float | None:
    """Return endpoint trend for timestamp/value samples; reject insufficient input."""
    if len(samples) < 2:
        return None
    start_t, start_c = samples[0]
    end_t, end_c = samples[-1]
    elapsed_h = (end_t - start_t) / 3600
    if elapsed_h <= 0:
        return None
    return (end_c - start_c) / elapsed_h


def predicted_temperature_60m(current_c: float, trend_c_per_h: float | None) -> float:
    """Predict one hour forward without pretending certainty for missing history."""
    return current_c if trend_c_per_h is None else current_c + trend_c_per_h
