"""Read-only contextual thermal learning from observed HA state samples."""

from __future__ import annotations

from collections.abc import Sequence

from .models import ThermalProfile

Sample = tuple[float, float, str, bool, float | None, float | None]


def _mean(values: list[float]) -> float | None:
    return None if not values else round(sum(values) / len(values), 3)


def learn_thermal_profile(samples: Sequence[Sample]) -> ThermalProfile:
    """Compare only adjacent, similarly contextual observations.

    Each sample contains timestamp, room temperature, HVAC mode, direct-sun
    geometry, average shade opening, outdoor temperature and irradiance.
    Weather values are retained for diagnostics; direct-sun and shade state are
    the conservative grouping keys.  Gaps and mode changes are never bridged.
    """
    sun: list[float] = []
    shaded: list[float] = []
    cooling: list[float] = []
    for previous, current in zip(samples, samples[1:]):
        before, after = previous[0], current[0]
        elapsed_h = (after - before) / 3600
        if not 1 / 60 <= elapsed_h <= 0.25 or previous[2] != current[2]:
            continue
        rate = (current[1] - previous[1]) / elapsed_h
        mode = current[2]
        if mode in {"cool", "dry"}:
            cooling.append(rate)
        elif mode in {"off", "fan_only"}:
            if current[3]:
                sun.append(rate)
            elif current[4] is not None and current[4] <= 10:
                shaded.append(rate)
    quality = "insufficient_history"
    if len(sun) >= 6 or len(shaded) >= 6 or len(cooling) >= 6:
        quality = "learning"
    if len(sun) >= 20 and len(shaded) >= 20 and len(cooling) >= 12:
        quality = "established"
    return ThermalProfile(_mean(sun), _mean(shaded), _mean(cooling), len(sun), len(shaded), len(cooling), quality)
