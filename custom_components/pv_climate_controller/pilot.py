"""Conservative, living-room-only PV pilot decisions.

The pilot produces one normalized start or stop request.  Service calls stay
outside this module so the safety logic is testable without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
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
    """Gentle PV pre-cooling with explicit ownership and compressor protection."""

    _MIN_START_TARGET_C = 23.0
    _MAX_START_TARGET_C = 24.0
    _DEEP_PRECOOL_AFTER_S = 30 * 60
    _TARGET_CHANGE_INTERVAL_S = 15 * 60
    _PV_WIND_DOWN_S = 10 * 60
    _MIN_OFF_TIME_S = 30 * 60

    def __init__(self, clock=monotonic) -> None:
        self._clock = clock
        self._demand_since: float | None = None
        self._cooling_started_at: float | None = None
        self._active_target_temperature_c: float | None = None
        self._last_target_change_at: float | None = None
        self._pv_missing_since: float | None = None
        self._last_stopped_at: float | None = None
        self._owns_cooling = False

    @property
    def owns_cooling(self) -> bool:
        return self._owns_cooling

    def release_ownership(self) -> None:
        """Leave an externally controlled device untouched."""
        self._owns_cooling = False
        self._cooling_started_at = None
        self._active_target_temperature_c = None
        self._last_target_change_at = None
        self._pv_missing_since = None

    def mark_sent(self, action: PilotAction) -> None:
        """Record only a command accepted by the guarded write boundary."""
        now = self._clock()
        if action.action == "start":
            self._owns_cooling = True
            self._cooling_started_at = now
            self._active_target_temperature_c = action.target_temperature_c
            self._last_target_change_at = now
            self._pv_missing_since = None
        elif action.action == "adjust":
            self._active_target_temperature_c = action.target_temperature_c
            self._last_target_change_at = now
        elif action.action == "stop":
            self.release_ownership()
            self._demand_since = None
            self._last_stopped_at = now

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
        # A split unit modulates best when it is allowed to settle.  Start at
        # the relaxed whole-degree comfort level (23–24 °C).  Only sustained,
        # genuinely high surplus may request one quiet 1 °C step later.
        start_target = min(self._MAX_START_TARGET_C, max(self._MIN_START_TARGET_C, float(ceil(zone.comfort_temperature))))
        deep_precool_target = start_target - 1.0
        strong_pv = export_power_w is not None and export_power_w >= 2 * config.min_pv_surplus_w
        needs_cooling = hard_limit or (pv_available and temperature_c > start_target)

        if climate_mode == "cool" and not self._owns_cooling:
            return PilotAction("none", None, "external_climate_control", "Klimagerät wird extern gesteuert; Pilot greift nicht ein.")
        if not self._owns_cooling:
            if not needs_cooling:
                self._demand_since = None
                return PilotAction("none", None, "pv_or_thermal_need_missing", "Kein PV-gestützter oder thermischer Kühlbedarf.")
            if not hard_limit and self._last_stopped_at is not None and now - self._last_stopped_at < self._MIN_OFF_TIME_S:
                return PilotAction("none", None, "pilot_resting", "Wohnzimmer-Pilot hält die Kompressor-Ruhezeit ein.")
            if hard_limit:
                return PilotAction("start", start_target, "hard_temperature_limit", f"Harte Temperaturgrenze erreicht; Kühlung startet sanft bei {start_target:.0f} °C.")
            if self._demand_since is None:
                self._demand_since = now
            if now - self._demand_since < 600:
                return PilotAction("none", None, "pilot_demand_stabilizing", "PV-Kühlbedarf wird zehn Minuten auf Stabilität geprüft.")
            return PilotAction("start", start_target, "pv_preconditioning", f"PV-Überschuss startet eine ruhige Vorkühlung bei {start_target:.0f} °C.")

        if climate_mode != "cool":
            self.release_ownership()
            return PilotAction("none", None, "pilot_start_unconfirmed", "Pilotstart ist am Klimagerät noch nicht bestätigt.")
        runtime_s = 0.0 if self._cooling_started_at is None else now - self._cooling_started_at
        target_change_due = self._last_target_change_at is None or now - self._last_target_change_at >= self._TARGET_CHANGE_INTERVAL_S
        desired_target = start_target
        if strong_pv and runtime_s >= self._DEEP_PRECOOL_AFTER_S and temperature_c > start_target + 0.5:
            desired_target = deep_precool_target

        if not pv_available and not hard_limit:
            if self._pv_missing_since is None:
                self._pv_missing_since = now
            if self._active_target_temperature_c != start_target:
                return PilotAction("adjust", start_target, "pv_wind_down", f"PV-Überschuss endet; Solltemperatur wird sanft auf {start_target:.0f} °C angehoben.")
            if now - self._pv_missing_since >= self._PV_WIND_DOWN_S:
                return PilotAction("stop", None, "pv_surplus_ended", "PV-Überschuss bleibt aus; sanfter Auslauf ist beendet.")
        else:
            self._pv_missing_since = None
            if self._active_target_temperature_c != desired_target and target_change_due:
                return PilotAction("adjust", desired_target, "pilot_soft_target_adjustment", "Stabiler PV-Überschuss erlaubt eine einzelne, ruhige Sollwertstufe.")

        # Reaching the setpoint is deliberately not a stop criterion while
        # export remains available.  The inverter can modulate at the settled
        # target and consume PV instead of being forced into short cycles.
        return PilotAction("none", None, "pilot_cooling_active", "Wohnzimmer wird mit PV ruhig und langlaufend moduliert.")


def living_room_pilot_eligible(config: ControllerConfig, granted_stages: int) -> tuple[bool, str]:
    """Allow the explicit living-room pilot; an EMS grant is optional."""
    if config.shadow_mode:
        return False, "shadow_mode"
    if config.zone is None:
        return False, "zone_missing"
    if config.zone.name.strip().casefold() != "wohnzimmer":
        return False, "pilot_living_room_only"
    if config.ems_granted_stages_entity_id is not None and granted_stages < 1:
        return False, "ems_grant_missing"
    return True, "pilot_eligible"
