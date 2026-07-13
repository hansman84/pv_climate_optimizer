"""Pure and deterministic zone demand evaluation."""

from __future__ import annotations

from .const import ZoneState
from .models import ZoneConfig, ZoneDecision, ZoneInput


def evaluate_zone(config: ZoneConfig, sample: ZoneInput) -> ZoneDecision:
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
    return ZoneDecision(config.zone_id, ZoneState.REQUESTED if demand else ZoneState.IDLE, demand, score, demand, reason_code, reason_text)
