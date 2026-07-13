"""Pure thermal-reserve calculations for Shadow Mode learning."""

from __future__ import annotations

from .models import ZoneConfig, ZoneForecast


def minutes_until_threshold(current_c: float, threshold_c: float, trend_c_per_h: float | None) -> float | None:
    """Return time to a rising threshold; never invent a time for flat/cooling data."""
    if current_c >= threshold_c:
        return 0.0
    if trend_c_per_h is None or trend_c_per_h <= 0:
        return None
    return round(60.0 * (threshold_c - current_c) / trend_c_per_h, 1)


def build_thermal_budget(config: ZoneConfig, temperature_c: float | None, forecast: ZoneForecast) -> dict[str, float | None]:
    """Describe thermal reserve using only observed room-temperature history."""
    if temperature_c is None or forecast.data_quality not in {"valid", "insufficient_history"}:
        return {"comfort_reserve_c": None, "hard_limit_reserve_c": None, "minutes_to_comfort": None, "minutes_to_hard_limit": None, "priority_bonus": 0.0}
    minutes_to_comfort = minutes_until_threshold(temperature_c, config.comfort_temperature, forecast.trend_c_per_h)
    minutes_to_hard = minutes_until_threshold(temperature_c, config.hard_max_temperature, forecast.trend_c_per_h)
    # A hard-limit breach within an hour dominates ordinary degree-based demand.
    priority_bonus = 0.0
    if minutes_to_hard is not None and minutes_to_hard <= 60:
        priority_bonus = 150.0 - minutes_to_hard
    elif minutes_to_comfort is not None and minutes_to_comfort <= 60:
        priority_bonus = 40.0 - minutes_to_comfort / 2
    return {
        "comfort_reserve_c": round(config.comfort_temperature - temperature_c, 2),
        "hard_limit_reserve_c": round(config.hard_max_temperature - temperature_c, 2),
        "minutes_to_comfort": minutes_to_comfort,
        "minutes_to_hard_limit": minutes_to_hard,
        "priority_bonus": round(max(0.0, priority_bonus), 2),
    }
