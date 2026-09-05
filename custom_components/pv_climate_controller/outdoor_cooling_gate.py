"""Pure, transport-free outdoor cooling gate for the Wohnzimmer pilot.

This module is intentionally a single, deterministic function.  It must not
import Home Assistant, must not perform IO, and must never raise.  The
intention is that V2 callers can ask one question and get one explainable
answer, both in shadow mode and in production.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutdoorForecast:
    """A normalized, time-ordered list of forecast hours.

    Each entry carries the same shape the HA weather service returns.  The
    gate only inspects ``temperature`` and ``precipitation_probability``.
    """

    hours: tuple[dict, ...]

    def __post_init__(self) -> None:
        for entry in self.hours:
            if not isinstance(entry, dict):
                raise ValueError("forecast hours must be dictionaries")
            if "temperature" not in entry and "precipitation_probability" not in entry:
                raise ValueError("forecast hours need temperature or precipitation_probability")


@dataclass(frozen=True, slots=True)
class OutdoorLive:
    """Live outdoor telemetry, sourced from the same weather entity."""

    temperature_c: float | None
    uv_index: float | None
    cloud_coverage_pct: float | None

    def __post_init__(self) -> None:
        for name, value in (
            ("temperature_c", self.temperature_c),
            ("uv_index", self.uv_index),
            ("cloud_coverage_pct", self.cloud_coverage_pct),
        ):
            if value is not None and not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric or None")


@dataclass(frozen=True, slots=True)
class OutdoorGateInputs:
    """One bounded input set the gate evaluates deterministically."""

    room_temperature_c: float | None
    comfort_temperature_c: float
    relaxation_band_c: float
    no_active_cooling_c: float
    rain_hold_probability_pct: float
    forecast: OutdoorForecast
    live: OutdoorLive
    pv_forecast_w: float | None
    pv_boost_extra_w: float

    def __post_init__(self) -> None:
        if not isinstance(self.relaxation_band_c, (int, float)) or self.relaxation_band_c <= 0:
            raise ValueError("relaxation_band_c must be a positive number")
        if not isinstance(self.no_active_cooling_c, (int, float)) or self.no_active_cooling_c <= 0:
            raise ValueError("no_active_cooling_c must be a positive number")
        if not 0 <= self.rain_hold_probability_pct <= 100:
            raise ValueError("rain_hold_probability_pct must be 0..100")
        if not isinstance(self.comfort_temperature_c, (int, float)):
            raise ValueError("comfort_temperature_c must be numeric")


@dataclass(frozen=True, slots=True)
class OutdoorGateDecision:
    """The only possible answer; never ``None``, never silent."""

    decision: str  # one of: "hold", "comfort", "pv_boosted", "rain_hold"
    relaxation_target_c: float | None
    today_max_outdoor_c: float | None
    reason_code: str
    reason_text: str
    gates: dict[str, bool]

    def __post_init__(self) -> None:
        allowed = {"hold", "comfort", "pv_boosted", "rain_hold"}
        if self.decision not in allowed:
            raise ValueError(f"decision must be one of {sorted(allowed)}")
        if not self.reason_code.strip() or not self.reason_text.strip():
            raise ValueError("decision needs reason_code and reason_text")


def _safe_max_today_c(forecast: OutdoorForecast) -> float | None:
    """Return the maximum temperature of the first 24 forecast hours, or ``None``."""
    if not forecast.hours:
        return None
    window = forecast.hours[:24]
    values: list[float] = []
    for entry in window:
        value = entry.get("temperature")
        if isinstance(value, (int, float)):
            values.append(float(value))
    if not values:
        return None
    return max(values)


def _rain_3h_streak(forecast: OutdoorForecast, threshold_pct: float) -> bool:
    """Return True when at least three consecutive forecast hours exceed the threshold."""
    if not forecast.hours:
        return False
    streak = 0
    for entry in forecast.hours[:12]:
        value = entry.get("precipitation_probability")
        if isinstance(value, (int, float)) and float(value) >= threshold_pct:
            streak += 1
            if streak >= 3:
                return True
        else:
            streak = 0
    return False


# An indoor room clearly above comfort (solar load through glazing) outweighs
# a mild outdoor day: acute need is served, only preventive starts are held.
GATE_ACUTE_BREACH_MARGIN_C = 0.3


def evaluate_outdoor_cooling_gate(inputs: OutdoorGateInputs) -> OutdoorGateDecision:
    """Decide the Wohnzimmer cooling gate for one evaluation cycle.

    The function is pure: given the same inputs it returns the same decision.
    """

    comfort = float(inputs.comfort_temperature_c)
    room = inputs.room_temperature_c
    today_max = _safe_max_today_c(inputs.forecast)
    live_temp = inputs.live.temperature_c
    uv = inputs.live.uv_index
    cloud = inputs.live.cloud_coverage_pct

    gates = {
        "forecast_available": today_max is not None,
        "live_temperature_available": live_temp is not None,
        "room_temperature_available": room is not None,
        "today_cool_enough": today_max is not None and today_max < comfort - inputs.no_active_cooling_c,
        "today_warm_enough": today_max is not None and today_max >= comfort + inputs.no_active_cooling_c,
        "outdoor_equilibrium": (
            room is not None
            and live_temp is not None
            and (room - live_temp) < inputs.relaxation_band_c
        ),
        "strong_sun": (uv is not None and uv >= 6.0) or (cloud is not None and cloud < 35.0),
        "pv_boost": (
            inputs.pv_forecast_w is not None
            and inputs.pv_boost_extra_w >= 0
            and inputs.pv_forecast_w >= 400.0 + inputs.pv_boost_extra_w
        ),
        "rain_streak": _rain_3h_streak(inputs.forecast, inputs.rain_hold_probability_pct),
    }

    relaxation_target: float | None = None
    equilibrium_target = (
        live_temp - 1.0
        if live_temp is not None and room is not None and (room - live_temp) < inputs.relaxation_band_c
        else None
    )
    if equilibrium_target is not None and equilibrium_target < comfort:
        relaxation_target = equilibrium_target
    elif (
        gates["today_cool_enough"]
        and live_temp is not None
        and room is not None
        and (live_temp - 1.0) < comfort
    ):
        relaxation_target = live_temp - 1.0

    if gates["rain_streak"]:
        return OutdoorGateDecision(
            decision="rain_hold",
            relaxation_target_c=None,
            today_max_outdoor_c=today_max,
            reason_code="outdoor_rain_streak",
            reason_text=(
                "Wohnzimmer-Komfort bleibt inaktiv: drei aufeinanderfolgende Forecast-Stunden "
                f"mit mindestens {inputs.rain_hold_probability_pct:.0f}% Niederschlagswahrscheinlichkeit."
            ),
            gates=gates,
        )

    if gates["today_cool_enough"]:
        # A mild outdoor day suppresses *preventive* cooling.  It must never
        # block *acute* indoor need caused by solar load through the large
        # glazing: once the measured room is clearly above comfort, comfort
        # cooling is allowed again (2026-09-05 household finding).
        acute_breach = room is not None and room > comfort + GATE_ACUTE_BREACH_MARGIN_C
        if not acute_breach:
            return OutdoorGateDecision(
                decision="hold",
                relaxation_target_c=relaxation_target,
                today_max_outdoor_c=today_max,
                reason_code="outdoor_today_cool",
                reason_text=(
                    f"Wohnzimmer-Komfort hält: Tagesmaximum {today_max:.1f} °C liegt unter Komfort "
                    f"({comfort:.1f} °C) − {inputs.no_active_cooling_c:.1f} °C; Außenluft reicht."
                ),
                gates=gates,
            )

    if gates["outdoor_equilibrium"] and not gates["strong_sun"]:
        return OutdoorGateDecision(
            decision="hold",
            relaxation_target_c=relaxation_target,
            today_max_outdoor_c=today_max,
            reason_code="outdoor_equilibrium",
            reason_text=(
                f"Wohnzimmer-Komfort entspannt: Innentemperatur {room:.1f} °C liegt weniger als "
                f"{inputs.relaxation_band_c:.1f} °C über der Außentemperatur {live_temp:.1f} °C; "
                "kein aktiver Kühlbedarf."
            ),
            gates=gates,
        )

    if gates["pv_boost"] and gates["today_warm_enough"]:
        return OutdoorGateDecision(
            decision="pv_boosted",
            relaxation_target_c=None,
            today_max_outdoor_c=today_max,
            reason_code="outdoor_pv_boost",
            reason_text=(
                f"Wohnzimmer-PV-Boost aktiv: Tagesmaximum {today_max:.1f} °C und "
                f"PV-Prognose {inputs.pv_forecast_w:.0f} W ≥ {400.0 + inputs.pv_boost_extra_w:.0f} W; "
                "zusätzliche −1 °C-Stufe mit Auto-Lüfter."
            ),
            gates=gates,
        )

    return OutdoorGateDecision(
        decision="comfort",
        relaxation_target_c=relaxation_target,
        today_max_outdoor_c=today_max,
        reason_code="outdoor_comfort",
        reason_text=(
            "Wohnzimmer-Komfort aktiv: "
            f"Tagesmaximum {today_max if today_max is not None else float('nan'):.1f} °C, "
            f"Außentemperatur {live_temp if live_temp is not None else float('nan'):.1f} °C, "
            f"PV-Prognose {inputs.pv_forecast_w if inputs.pv_forecast_w is not None else float('nan'):.0f} W."
        ),
        gates=gates,
    )
