"""Conservative, living-room-only PV pilot decisions.

The pilot produces one normalized start or stop request.  Service calls stay
outside this module so the safety logic is testable without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from time import monotonic

from .models import ControllerConfig, ThermalProfile


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
    _SETTLE_STOP_DELAY_S = 10 * 60

    def __init__(self, clock=monotonic, expected_zone_name: str = "Wohnzimmer", display_name: str | None = None) -> None:
        self._clock = clock
        self._expected_zone_name = expected_zone_name
        self._display_name = display_name or expected_zone_name
        self._demand_since: float | None = None
        self._cooling_started_at: float | None = None
        self._active_target_temperature_c: float | None = None
        self._last_target_change_at: float | None = None
        self._pv_missing_since: float | None = None
        self._settled_since: float | None = None
        self._last_stopped_at: float | None = None
        self._owns_cooling = False
        self._takeover_requested = False
        self._observed_snapshot: tuple[str | None, float | None, str | None, str | None] | None = None
        self._expected_snapshot: tuple[str | None, float | None, str | None, str | None] | None = None

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
        self._settled_since = None
        self._takeover_requested = False
        self._observed_snapshot = None
        self._expected_snapshot = None

    def request_takeover(self) -> None:
        """Accept a one-shot handover from the dashboard button."""
        self._takeover_requested = True

    def mark_sent(self, action: PilotAction) -> None:
        """Record only a command accepted by the guarded write boundary."""
        now = self._clock()
        if action.action == "start":
            self._owns_cooling = True
            self._cooling_started_at = now
            self._active_target_temperature_c = action.target_temperature_c
            self._last_target_change_at = now
            self._pv_missing_since = None
            self._settled_since = None
            self._expected_snapshot = ("cool", action.target_temperature_c, None if self._observed_snapshot is None else self._observed_snapshot[2], None if self._observed_snapshot is None else self._observed_snapshot[3])
        elif action.action == "adjust":
            self._active_target_temperature_c = action.target_temperature_c
            self._last_target_change_at = now
            self._expected_snapshot = ("cool", action.target_temperature_c, None if self._observed_snapshot is None else self._observed_snapshot[2], None if self._observed_snapshot is None else self._observed_snapshot[3])
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
        thermal_profile: ThermalProfile | None = None,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
    ) -> PilotAction | None:
        """Return a PV-first action or a visible reason for doing nothing."""
        allowed, reason = living_room_pilot_eligible(config, granted_stages, self._expected_zone_name)
        if not allowed:
            return PilotAction("none", None, reason, f"{self._display_name}-Pilot ist derzeit gesperrt.")
        zone = config.zone
        assert zone is not None
        if temperature_c is None or not zone.minimum_plausible_temperature_c <= temperature_c <= zone.maximum_plausible_temperature_c:
            return PilotAction("none", None, "temperature_invalid", "Raumtemperatur ist ungültig oder nicht verfügbar.")
        if climate_mode in {None, "unknown", "unavailable"}:
            return PilotAction("none", None, "climate_unavailable", "Klimagerät ist nicht verfügbar.")

        now = self._clock()
        snapshot = (climate_mode, climate_target_temperature_c, climate_fan_mode, climate_swing_mode)
        if self._owns_cooling and self._manual_change_detected(snapshot):
            self.release_ownership()
            return PilotAction("none", None, "manual_control_resumed", "Manuelle Änderung erkannt; Wohnzimmer-Pilot hat die Kontrolle zurückgegeben.")
        pv_available = export_power_w is not None and export_power_w >= config.min_pv_surplus_w
        hard_limit = temperature_c >= zone.hard_max_temperature
        # The configured comfort value is the thermal promise, even when the
        # indoor unit accepts whole degrees only.  For a 23.5 °C comfort
        # threshold, cool at 23 °C until the room reaches the threshold, then
        # lift to 24 °C for quiet holding instead of silently treating 24 °C
        # as the threshold.
        hold_target = min(self._MAX_START_TARGET_C, max(self._MIN_START_TARGET_C, float(ceil(zone.comfort_temperature))))
        fractional_comfort = abs(zone.comfort_temperature - round(zone.comfort_temperature)) > 0.01
        cool_target = max(self._MIN_START_TARGET_C, hold_target - 1.0) if fractional_comfort else hold_target
        deep_precool_target = hold_target - 1.0
        strong_pv = export_power_w is not None and export_power_w >= 2 * config.min_pv_surplus_w
        needs_cooling = hard_limit or (pv_available and temperature_c > zone.comfort_temperature)

        if climate_mode == "cool" and not self._owns_cooling:
            if not self._takeover_requested:
                return PilotAction("none", None, "external_climate_control", "Klimagerät wird extern gesteuert; Pilot greift nicht ein.")
            self._adopt_external_cooling(now, snapshot)
        if not self._owns_cooling:
            if not needs_cooling:
                self._demand_since = None
                return PilotAction("none", None, "pv_or_thermal_need_missing", "Kein PV-gestützter oder thermischer Kühlbedarf.")
            if not hard_limit and self._last_stopped_at is not None and now - self._last_stopped_at < self._MIN_OFF_TIME_S:
                return PilotAction("none", None, "pilot_resting", "Wohnzimmer-Pilot hält die Kompressor-Ruhezeit ein.")
            if hard_limit:
                return PilotAction("start", cool_target, "hard_temperature_limit", f"Harte Temperaturgrenze erreicht; Kühlung startet sanft bei {cool_target:.0f} °C.")
            if self._demand_since is None:
                self._demand_since = now
            if now - self._demand_since < 600:
                return PilotAction("none", None, "pilot_demand_stabilizing", "PV-Kühlbedarf wird zehn Minuten auf Stabilität geprüft.")
            return PilotAction("start", cool_target, "pv_preconditioning", f"PV-Überschuss startet eine ruhige Vorkühlung bei {cool_target:.0f} °C.")

        if climate_mode != "cool":
            self.release_ownership()
            return PilotAction("none", None, "pilot_start_unconfirmed", "Pilotstart ist am Klimagerät noch nicht bestätigt.")
        runtime_s = 0.0 if self._cooling_started_at is None else now - self._cooling_started_at
        target_change_due = self._last_target_change_at is None or now - self._last_target_change_at >= self._TARGET_CHANGE_INTERVAL_S
        desired_target = cool_target if temperature_c > zone.comfort_temperature else hold_target
        if strong_pv and runtime_s >= self._DEEP_PRECOOL_AFTER_S and temperature_c > hold_target + 0.5:
            desired_target = deep_precool_target

        if not pv_available and not hard_limit:
            self._settled_since = None
            if self._pv_missing_since is None:
                self._pv_missing_since = now
            if self._active_target_temperature_c != hold_target:
                return PilotAction("adjust", hold_target, "pv_wind_down", f"PV-Überschuss endet; Solltemperatur wird sanft auf {hold_target:.0f} °C angehoben.")
            if now - self._pv_missing_since >= self._PV_WIND_DOWN_S:
                return PilotAction("stop", None, "pv_surplus_ended", "PV-Überschuss bleibt aus; sanfter Auslauf ist beendet.")
        else:
            self._pv_missing_since = None
            if self._active_target_temperature_c != desired_target and target_change_due:
                return PilotAction("adjust", desired_target, "pilot_soft_target_adjustment", "Stabiler PV-Überschuss erlaubt eine einzelne, ruhige Sollwertstufe.")

        # PV alone is not a reason to cool indefinitely.  When the room has
        # reached its currently planned target and no solar rebound is likely,
        # allow a short settle period, then switch the unit off.  Productive
        # PV may instead retain the relaxed whole-degree target while the room
        # is still inside its comfort band: this avoids needless compressor
        # cycling without silently overcooling the room.
        at_target = temperature_c <= desired_target
        rebound_expected = self._rebound_expected(thermal_profile, direct_sun, irradiance_w_m2)
        pv_holding_allowed = pv_available and temperature_c >= zone.comfort_temperature - 0.5
        if at_target and not rebound_expected and not pv_holding_allowed:
            if self._active_target_temperature_c != hold_target and target_change_due:
                self._settled_since = now
                return PilotAction("adjust", hold_target, "pilot_settling", f"Kühlziel erreicht; Solltemperatur wird zum ruhigen Auslaufen auf {hold_target:.0f} °C angehoben.")
            if self._settled_since is None:
                self._settled_since = now
                return PilotAction("none", None, "pilot_settling", "Kühlziel erreicht; Pilot prüft zehn Minuten lang, ob der Raum stabil bleibt.")
            if now - self._settled_since >= self._SETTLE_STOP_DELAY_S:
                return PilotAction("stop", None, "thermal_target_reached", "Kühlziel ist stabil erreicht; ohne erwartete Wiederaufheizung wird das Klimagerät ausgeschaltet.")
        else:
            self._settled_since = None

        return PilotAction("none", None, "pilot_cooling_active", "Wohnzimmer wird mit PV ruhig und langlaufend moduliert.")

    def _adopt_external_cooling(self, now: float, snapshot: tuple[str | None, float | None, str | None, str | None]) -> None:
        """Treat an explicitly handed-over cooling run as pilot-owned from now on."""
        self._owns_cooling = True
        self._cooling_started_at = now
        self._active_target_temperature_c = None
        self._last_target_change_at = None
        self._pv_missing_since = None
        self._settled_since = None
        self._takeover_requested = False
        self._observed_snapshot = snapshot
        self._expected_snapshot = None

    def _manual_change_detected(self, snapshot: tuple[str | None, float | None, str | None, str | None]) -> bool:
        """Differentiate acknowledged pilot setpoints from the next user change."""
        if self._expected_snapshot is not None:
            if snapshot == self._expected_snapshot:
                self._observed_snapshot = snapshot
                self._expected_snapshot = None
                return False
            return self._observed_snapshot is not None and snapshot != self._observed_snapshot
        if self._observed_snapshot is None:
            self._observed_snapshot = snapshot
            return False
        return snapshot != self._observed_snapshot

    @staticmethod
    def _rebound_expected(
        profile: ThermalProfile | None,
        direct_sun: bool,
        irradiance_w_m2: float | None,
    ) -> bool:
        """Keep cooling only where solar gain or learned passive warming supports it."""
        if direct_sun:
            return True
        if irradiance_w_m2 is not None and irradiance_w_m2 >= 250:
            return True
        if profile is None:
            return False
        trends = (profile.passive_sun_trend_c_per_h, profile.passive_shaded_trend_c_per_h)
        return any(trend is not None and trend >= 0.3 for trend in trends)


def living_room_pilot_eligible(config: ControllerConfig, granted_stages: int, expected_zone_name: str = "Wohnzimmer") -> tuple[bool, str]:
    """Allow one explicitly named productive room pilot; an EMS grant is optional."""
    if config.shadow_mode:
        return False, "shadow_mode"
    if config.zone is None:
        return False, "zone_missing"
    if config.zone.name.strip().casefold() != expected_zone_name.casefold():
        return False, "pilot_living_room_only"
    if config.ems_granted_stages_entity_id is not None and granted_stages < 1:
        return False, "ems_grant_missing"
    return True, "pilot_eligible"
