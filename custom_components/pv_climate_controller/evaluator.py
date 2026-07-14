"""Pure and deterministic zone demand evaluation."""

from __future__ import annotations

from datetime import time

from .const import ZoneState
from .models import ZoneConfig, ZoneDecision, ZoneInput


def evaluate_zone(
    config: ZoneConfig,
    sample: ZoneInput,
    *,
    now: time | None = None,
    pv_surplus_available: bool = False,
) -> ZoneDecision:
    """Evaluate one zone without issuing commands or assuming EMS capacity."""
    if not sample.climate_available:
        return ZoneDecision(config.zone_id, ZoneState.UNAVAILABLE, False, 0.0, False, "climate_unavailable", "Klimagerät nicht verfügbar.")
    if sample.temperature_c is None:
        return ZoneDecision(config.zone_id, ZoneState.IDLE, False, 0.0, False, "temperature_invalid", "Raumtemperatur ist ungültig oder nicht verfügbar.")
    if not config.minimum_plausible_temperature_c <= sample.temperature_c <= config.maximum_plausible_temperature_c:
        return ZoneDecision(
            config.zone_id, ZoneState.DATA_QUALITY, False, 0.0, False,
            "temperature_implausible",
            "Raumtemperatur liegt außerhalb des konfigurierten plausiblen Bereichs.",
        )
    if sample.manual_override:
        return ZoneDecision(config.zone_id, ZoneState.MANUAL_OVERRIDE, False, 0.0, False, "manual_override", "Manueller Eingriff ist aktiv.")

    score = max(0.0, 25.0 * (sample.temperature_c - config.comfort_temperature))
    hard_limit = sample.temperature_c >= config.hard_max_temperature
    if hard_limit:
        score += 100.0
    demand = sample.temperature_c >= config.comfort_temperature
    reason_code = "hard_temperature_limit" if hard_limit else "comfort_temperature_exceeded" if demand else "comfort_temperature_ok"
    reason_text = "Harte Temperaturgrenze erreicht." if hard_limit else "Komforttemperatur überschritten." if demand else "Komforttemperatur eingehalten."
    base = ZoneDecision(config.zone_id, ZoneState.REQUESTED if demand else ZoneState.IDLE, demand, score, demand, reason_code, reason_text)
    return _apply_family_profile(config, sample, base, now, pv_surplus_available)


def _apply_family_profile(
    config: ZoneConfig,
    sample: ZoneInput,
    base: ZoneDecision,
    now: time | None,
    pv_surplus_available: bool,
) -> ZoneDecision:
    """Apply the family's comfort intent as a Shadow-Mode recommendation.

    The limits always win.  The profile is deliberately conservative: it only
    changes the recommendation shown in the plan, never a climate device.
    Defaults are visible and deterministic: living comfort 07:00–22:00,
    bedroom PV pre-cooling 15:00–21:00, sleep target from 21:00 is 23 °C.
    """
    if sample.temperature_c is None or base.reason_code == "hard_temperature_limit":
        return base
    local_time = now or time(12, 0)
    room = config.name.casefold()
    if "wohnzimmer" in room and not time(7, 0) <= local_time < time(22, 0):
        return ZoneDecision(
            config.zone_id, ZoneState.IDLE, False, 0.0, False,
            "family_living_night_limit",
            "Nachts kein Wohnzimmer-Komfortziel; nur die harte Temperaturgrenze schützt den Raum.",
            "living_night_hard_limit", None,
        )
    if "schlafzimmer" not in room and "kinderzimmer" not in room:
        return base
    sleep_target = 23.0
    precondition = time(15, 0) <= local_time < time(21, 0)
    sleep_window = local_time >= time(21, 0) or local_time < time(7, 0)
    if precondition:
        if pv_surplus_available and sample.temperature_c > sleep_target:
            return ZoneDecision(
                config.zone_id, ZoneState.REQUESTED, True,
                max(base.score, 50.0 + 25.0 * (sample.temperature_c - sleep_target)), True,
                "family_sleep_pv_precondition",
                "PV-Überschuss wird zum Vorkühlen für die Schlafzeit empfohlen.",
                "sleep_pv_precondition", sleep_target,
            )
        return ZoneDecision(
            config.zone_id, ZoneState.IDLE, False, 0.0, False,
            "family_sleep_waiting_for_pv",
            "Tagsüber kein Komfortziel; Vorkühlung startet nur bei PV-Überschuss.",
            "sleep_daytime_wait", sleep_target,
        )
    if sleep_window and sample.temperature_c > sleep_target:
        return ZoneDecision(
            config.zone_id, ZoneState.REQUESTED, True,
            max(base.score, 40.0 + 25.0 * (sample.temperature_c - sleep_target)), True,
            "family_sleep_target_exceeded",
            "Schlafziel von maximal 23 °C ist überschritten.",
            "sleep_target", sleep_target,
        )
    if sleep_window:
        return ZoneDecision(
            config.zone_id, ZoneState.IDLE, False, 0.0, False,
            "family_sleep_target_ok",
            "Schlafziel von maximal 23 °C ist eingehalten.",
            "sleep_target", sleep_target,
        )
    return ZoneDecision(
        config.zone_id, ZoneState.IDLE, False, 0.0, False,
        "family_sleep_daytime",
        "Tagsüber kein Komfortziel für diesen Schlafraum.",
        "sleep_daytime_wait", sleep_target,
    )
