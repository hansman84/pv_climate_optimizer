"""Small, deterministic forecasting helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextualForecast:
    """Room forecast enriched only by observed solar behaviour.

    ``horizon_h`` is the look-ahead the household configured for this room.
    A glazed living room heats quickly, so it can look further ahead (2 h) and
    start its pre-cool step earlier instead of reacting at the comfort limit.
    """

    predicted_temperature_60m_c: float | None
    trend_c_per_h: float | None
    confidence_adjustment: float
    thermal_factors: tuple[str, ...]
    horizon_h: float = 1.0


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


def predicted_temperature_60m(current_c: float, trend_c_per_h: float | None, horizon_h: float = 1.0) -> float:
    """Predict forward by ``horizon_h`` hours (default: the classic one hour).

    A room trend is a linear extrapolation; the caller decides how far ahead it
    is useful.  Missing history never pretends certainty.
    """
    return current_c if trend_c_per_h is None else current_c + trend_c_per_h * max(0.05, horizon_h)


def contextual_temperature_forecast(
    current_c: float | None,
    observed_trend_c_per_h: float | None,
    *,
    direct_sun: bool,
    shade_open_percent: float | None,
    irradiance_w_m2: float | None,
    passive_sun_trend_c_per_h: float | None,
    passive_shaded_trend_c_per_h: float | None,
    horizon_h: float = 1.0,
) -> ContextualForecast:
    """Blend the measured trend with the matching, learned solar context.

    The current room trend remains the primary signal.  Solar inputs only
    adjust it when the room has both a known sun/shade state and a learned
    passive response.  This prevents a new or incompletely configured room
    from receiving a speculative cooling request merely because the sun is out.
    """
    factors: list[str] = []

    def predict(trend_c_per_h: float | None) -> float:
        """Linear look-ahead over this room's configured horizon."""
        return round(predicted_temperature_60m(current_c, trend_c_per_h, horizon_h), 2)

    def _forecast(predicted_value: float | None, trend: float | None, adjustment: float, factors_in: tuple[str, ...]) -> ContextualForecast:
        return ContextualForecast(predicted_value, trend, adjustment, factors_in, horizon_h)

    if current_c is None or observed_trend_c_per_h is None:
        return _forecast(None, observed_trend_c_per_h, 0.0, ("Temperaturtrend wird noch gesammelt",))

    if not direct_sun:
        return _forecast(
            predict(observed_trend_c_per_h),
            observed_trend_c_per_h,
            0.0,
            ("keine direkte Sonne auf der Raumfassade",),
        )
    factors.append("direkte Sonne auf der Raumfassade")
    if irradiance_w_m2 is None:
        return _forecast(
            predict(observed_trend_c_per_h),
            observed_trend_c_per_h,
            0.0,
            tuple(factors + ["Strahlungswert fehlt"]),
        )
    if shade_open_percent is None:
        return _forecast(
            predict(observed_trend_c_per_h),
            observed_trend_c_per_h,
            0.0,
            tuple(factors + [f"Einstrahlung {round(irradiance_w_m2)} W/m²", "Beschattungszustand fehlt"]),
        )

    shade_open = min(100.0, max(0.0, shade_open_percent))
    sun_exposed = shade_open > 10.0
    learned_trend = passive_sun_trend_c_per_h if sun_exposed else passive_shaded_trend_c_per_h
    context_name = "Sonnenprofil" if sun_exposed else "Beschattungsprofil"
    factors.extend((f"Einstrahlung {round(irradiance_w_m2)} W/m²", f"Beschattung offen {round(shade_open)} %"))
    if learned_trend is None:
        return _forecast(
            predict(observed_trend_c_per_h),
            observed_trend_c_per_h,
            0.0,
            tuple(factors + [f"{context_name} wird noch gelernt"]),
        )

    # At weak irradiance the observed trend wins almost completely.  At strong
    # irradiance the matching learned profile may correct at most 0.5 °C/h,
    # keeping the forecast calm and avoiding a step response to passing clouds.
    irradiance_weight = min(1.0, max(0.0, (irradiance_w_m2 - 100.0) / 700.0))
    openness_weight = shade_open / 100.0 if sun_exposed else 1.0 - shade_open / 100.0
    blend = 0.2 + 0.45 * irradiance_weight * openness_weight
    adjustment = max(-0.5, min(0.5, (learned_trend - observed_trend_c_per_h) * blend))
    adjusted_trend = round(observed_trend_c_per_h + adjustment, 3)
    factors.append(f"gelerntes {context_name}: {round(learned_trend, 2)} °C/h")
    return _forecast(
        predict(adjusted_trend),
        adjusted_trend,
        round(min(0.25, 0.05 + 0.20 * irradiance_weight * openness_weight), 2),
        tuple(factors),
    )
