"""Conservative, living-room-only PV pilot decisions.

The pilot produces one normalized start or stop request.  Service calls stay
outside this module so the safety logic is testable without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from .models import ControllerConfig


@dataclass(frozen=True, slots=True)
class PilotAction:
    """One allowed state transition for the confirmed living-room device."""

    action: str
    target_temperature_c: float | None
    reason_code: str
    reason_text: str


class LivingRoomPilot:
    """PV pre-cooling with explicit ownership and compressor protection."""

    def __init__(self, clock=monotonic) -> None:
        self._clock = clock
        self._demand_since: float | None = None
        self._target_reached_since: float | None = None
        self._cooling_started_at: float | None = None
        self._owns_cooling = False

    @property
    def owns_cooling(self) -> bool:
        return self._owns_cooling

    def release_ownership(self) -> None:
        """Leave an externally controlled device untouched."""
        self._owns_cooling = False
        self._cooling_started_at = None
        self._target_reached_since = None

    def mark_sent(self, action: PilotAction) -> None:
        """Record only a command accepted by the guarded write boundary."""
        now = self._clock()
        if action.action == "start":
            self._owns_cooling = True
            self._cooling_started_at = now
        elif action.action == "stop":
            self.release_ownership()
            self._demand_since = None

    def decide(
        self,
        config: ControllerConfig,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        granted_stages: int,
        export_power_w: float | None,
    ) -> PilotAction | None:
        """Return a PV-first action or a visible reason for doing nothing."""
        allowed, reason = living_room_pilot_eligible(config, granted_stages)
        if not allowed:
            return PilotAction("none", None, reason, "Wohnzimmer-Pilot ist derzeit gesperrt.")
        zone = config.zone
        assert zone is not None
        if temperature_c is None or not zone.minimum_plausible_temperature_c <= temperature_c <= zone.maximum_plausible_temperature_c:
            return PilotAction("none", None, "temperature_invalid", "Raumtemperatur ist ungültig oder nicht verfügbar.")
        if climate_mode in {None, "unknown", "unavailable"}:
            return PilotAction("none", None, "climate_unavailable", "Klimagerät ist nicht verfügbar.")

        now = self._clock()
        pv_available = export_power_w is not None and export_power_w >= config.min_pv_surplus_w
        hard_limit = temperature_c >= zone.hard_max_temperature
        # Use the exported energy for a small, bounded thermal buffer.  The
        # target never drops by more than 1 °C below the selected comfort value.
        extra_cooling = 0.5 if pv_available else 0.3
        if pv_available and export_power_w >= 2 * config.min_pv_surplus_w:
            extra_cooling = 1.0
        target = max(16.0, round(zone.comfort_temperature - extra_cooling, 1))
        needs_cooling = hard_limit or (pv_available and temperature_c > target)

        if climate_mode == "cool" and not self._owns_cooling:
            return PilotAction("none", None, "external_climate_control", "Klimagerät wird extern gesteuert; Pilot greift nicht ein.")
        if not self._owns_cooling:
            if not needs_cooling:
                self._demand_since = None
                return PilotAction("none", None, "pv_or_thermal_need_missing", "Kein PV-gestützter oder thermischer Kühlbedarf.")
            if hard_limit:
                return PilotAction("start", target, "hard_temperature_limit", "Harte Temperaturgrenze erreicht; Kühlung wird angefordert.")
            if self._demand_since is None:
                self._demand_since = now
            if now - self._demand_since < 600:
                return PilotAction("none", None, "pilot_demand_stabilizing", "PV-Kühlbedarf wird zehn Minuten auf Stabilität geprüft.")
            return PilotAction("start", target, "pv_preconditioning", "PV-Überschuss wird zum begrenzten Vorkühlen genutzt.")

        if climate_mode != "cool":
            self.release_ownership()
            return PilotAction("none", None, "pilot_start_unconfirmed", "Pilotstart ist am Klimagerät noch nicht bestätigt.")
        if temperature_c <= target:
            if self._target_reached_since is None:
                self._target_reached_since = now
            ran_long_enough = self._cooling_started_at is not None and now - self._cooling_started_at >= 1200
            stable_target = now - self._target_reached_since >= 600
            if ran_long_enough and stable_target:
                return PilotAction("stop", None, "pilot_target_reached", "PV-Ziel erreicht und Mindestlaufzeit erfüllt.")
        else:
            self._target_reached_since = None
        if not pv_available and not hard_limit and self._cooling_started_at is not None and now - self._cooling_started_at >= 1200:
            return PilotAction("stop", None, "pv_surplus_ended", "PV-Überschuss beendet; Mindestlaufzeit ist erfüllt.")
        return PilotAction("none", None, "pilot_cooling_active", "Wohnzimmer wird innerhalb der Pilotgrenzen gekühlt.")


def living_room_pilot_eligible(config: ControllerConfig, granted_stages: int) -> tuple[bool, str]:
    """Allow only an explicitly named living-room pilot with a fresh grant."""
    if config.shadow_mode:
        return False, "shadow_mode"
    if config.zone is None:
        return False, "zone_missing"
    if config.zone.name.strip().casefold() != "wohnzimmer":
        return False, "pilot_living_room_only"
    if granted_stages < 1:
        return False, "ems_grant_missing"
    return True, "pilot_eligible"
