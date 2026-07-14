"""Pure helpers for room facade geometry and grouped roller shutters."""

from __future__ import annotations

from typing import Any


def normalize_zone_tuning(user_input: dict[str, Any]) -> dict[str, Any]:
    """Serialize tuning while keeping each azimuth paired with its cover group.

    Multiple covers on a wide sliding door are deliberately stored as one
    facade group.  Blank optional facades are removed together with their
    groups, preventing an index shift between geometry and shading data.
    """
    tuning = dict(user_input)
    azimuths: list[float] = []
    facade_shades: list[list[str]] = []
    # Keep the form selection by its original slot as well.  A cover selection
    # is useful configuration in its own right and must not disappear merely
    # because its optional azimuth has not been entered yet.
    facade_shade_defaults: list[list[str]] = []
    for azimuth_key, shade_key in (
        ("facade_azimuth_primary", "facade_shade_primary"),
        ("facade_azimuth_secondary", "facade_shade_secondary"),
        ("facade_azimuth_tertiary", "facade_shade_tertiary"),
    ):
        raw = tuning.pop(azimuth_key, "")
        shades = [entity for entity in tuning.pop(shade_key, []) if isinstance(entity, str)]
        facade_shade_defaults.append(shades)
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 359:
            azimuths.append(value)
            facade_shades.append(shades)
    tuning["facade_azimuths"] = azimuths
    tuning["facade_shade_entity_ids"] = facade_shades
    tuning["facade_shade_defaults"] = facade_shade_defaults
    raw_cutoff = tuning.get("overhang_cutoff_elevation", "")
    try:
        cutoff = float(str(raw_cutoff).strip())
    except (TypeError, ValueError):
        cutoff = None
    tuning["overhang_cutoff_elevation"] = cutoff if cutoff is not None and 0 <= cutoff <= 90 else None
    return tuning
