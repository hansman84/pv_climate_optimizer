"""Conservative, living-room-only PV pilot decisions.

The pilot produces one normalized start or stop request.  Service calls stay
outside this module so the safety logic is testable without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor
from time import monotonic

from .models import ControllerConfig, ThermalProfile


@dataclass(frozen=True, slots=True)
class PilotAction:
    """One allowed state transition for the confirmed living-room device."""

    action: str
    target_temperature_c: float | None
    reason_code: str
    reason_text: str
    planned_target_temperature_c: float | None = None


class LivingRoomPilot:
    """Gentle PV pre-cooling with explicit ownership and compressor protection."""

    _MIN_START_TARGET_C = 23.0
    _MAX_START_TARGET_C = 24.0
    _THERMAL_RELIEF_TARGET_C = 25.0
    _DEEP_PRECOOL_AFTER_S = 30 * 60
    _TARGET_CHANGE_INTERVAL_S = 15 * 60
    _COMMAND_ACK_GRACE_S = 2 * 60
    # Let an already-running inverter settle at its relaxed target before
    # stopping after PV disappears.  This retains the cool room with only low
    # compressor demand, but does not turn a PV run into indefinite grid use.
    _PV_WIND_DOWN_S = 30 * 60
    _PV_WIND_DOWN_FAST_STEP_S = 5 * 60
    _PV_WIND_DOWN_SLOW_STEP_S = 15 * 60
    _MIN_OFF_TIME_S = 30 * 60
    _SETTLE_STOP_DELAY_S = 10 * 60
    _OVERSHOOT_MARGIN_C = 0.4
    _RAPID_COOLING_C_PER_H = -0.35
    _OVERSHOOT_CONFIRMATION_S = 2 * 60
    _PV_CAPACITY_TARGET_INTERVAL_S = 5 * 60

    def __init__(
        self,
        clock=monotonic,
        expected_zone_name: str = "Wohnzimmer",
        display_name: str | None = None,
        overshoot_margin_c: float | None = None,
        overshoot_confirmation_s: float | None = None,
        thermal_relief_observation_s: float | None = None,
        min_start_target_c: float | None = None,
        max_start_target_c: float | None = None,
        thermal_relief_target_c: float | None = None,
    ) -> None:
        self._clock = clock
        self._expected_zone_name = expected_zone_name
        self._display_name = display_name or expected_zone_name
        self._overshoot_margin_c = self._OVERSHOOT_MARGIN_C if overshoot_margin_c is None else overshoot_margin_c
        self._overshoot_confirmation_s = self._OVERSHOOT_CONFIRMATION_S if overshoot_confirmation_s is None else overshoot_confirmation_s
        self._thermal_relief_observation_s = self._SETTLE_STOP_DELAY_S if thermal_relief_observation_s is None else thermal_relief_observation_s
        self._min_start_target_c = self._MIN_START_TARGET_C if min_start_target_c is None else min_start_target_c
        self._max_start_target_c = self._MAX_START_TARGET_C if max_start_target_c is None else max_start_target_c
        self._thermal_relief_target_c = self._THERMAL_RELIEF_TARGET_C if thermal_relief_target_c is None else thermal_relief_target_c
        self._demand_since: float | None = None
        self._cooling_started_at: float | None = None
        self._active_target_temperature_c: float | None = None
        self._last_target_change_at: float | None = None
        self._last_heat_pump_relief_at: float | None = None
        self._model_target_offset_c = 0.0
        self._pending_model_target_offset_c: float | None = None
        self._expected_snapshot_at: float | None = None
        self._pv_missing_since: float | None = None
        self._settled_since: float | None = None
        self._overcooling_since: float | None = None
        self._thermal_relief_since: float | None = None
        self._last_stopped_at: float | None = None
        self._owns_cooling = False
        self._manual_override_active = False
        self._takeover_requested = False
        self._sunset_takeover_active = False
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
        self._last_heat_pump_relief_at = None
        self._model_target_offset_c = 0.0
        self._pending_model_target_offset_c = None
        self._expected_snapshot_at = None
        self._pv_missing_since = None
        self._settled_since = None
        self._overcooling_since = None
        self._thermal_relief_since = None
        self._takeover_requested = False
        self._observed_snapshot = None
        self._expected_snapshot = None

    def request_takeover(self) -> None:
        """Accept a one-shot handover from the dashboard button."""
        self._takeover_requested = True
        self._manual_override_active = False

    @staticmethod
    def _dynamic_room_target(
        temperature_c: float,
        comfort_temperature_c: float,
        hard_max_temperature_c: float,
        min_target_c: float,
        max_target_c: float,
    ) -> float:
        """Map room-temperature error proportionally onto the device range."""
        control_band_c = max(0.5, hard_max_temperature_c - comfort_temperature_c)
        demand = min(1.0, max(0.0, (temperature_c - comfort_temperature_c) / control_band_c))
        continuous_target = max_target_c - demand * (max_target_c - min_target_c)
        whole_degree_target = float(floor(continuous_target + 0.5))
        if temperature_c >= comfort_temperature_c - 0.75:
            quiet_hold_target = min(max_target_c, max(min_target_c, float(ceil(comfort_temperature_c))))
            whole_degree_target = min(whole_degree_target, quiet_hold_target)
        return min(max_target_c, max(min_target_c, whole_degree_target))

    @staticmethod
    def _pv_capacity_target(
        *,
        export_power_w: float | None,
        minimum_surplus_w: float,
        outdoor_unit_power_w: float | None,
        min_target_c: float,
        max_target_c: float,
    ) -> float | None:
        """Map usable net export across the configured device target range."""
        if export_power_w is None or export_power_w <= minimum_surplus_w:
            return None
        # The measured outdoor-unit draw is the best available scale for the
        # additional compressor work a lower target can request.  Without it,
        # do not invent an aggressive PV capacity target.
        if outdoor_unit_power_w is None or outdoor_unit_power_w <= 0:
            return None
        usable_headroom_w = export_power_w - minimum_surplus_w
        utilization = min(1.0, max(0.0, usable_headroom_w / outdoor_unit_power_w))
        continuous_target = max_target_c - utilization * (max_target_c - min_target_c)
        return min(max_target_c, max(min_target_c, float(floor(continuous_target + 0.5))))

    @staticmethod
    def _model_adjusted_target(
        base_target_c: float,
        *,
        temperature_c: float,
        comfort_temperature_c: float,
        min_target_c: float,
        max_target_c: float,
        temperature_trend_c_per_h: float | None,
        predicted_temperature_60m_c: float | None,
        thermal_profile: ThermalProfile | None,
        direct_sun: bool,
        irradiance_w_m2: float | None,
        shade_open_percent: float | None,
        active_cooling_zone_count: int,
        target_offset_c: float,
    ) -> tuple[float, tuple[str, ...], float]:
        """Apply one explainable feedback step from the observed room model."""
        factors: list[str] = []
        target = base_target_c + target_offset_c
        above_comfort = temperature_c > comfort_temperature_c + 0.2
        below_comfort = temperature_c < comfort_temperature_c - 0.2
        forecast_too_warm = predicted_temperature_60m_c is not None and predicted_temperature_60m_c > comfort_temperature_c + 0.3
        forecast_too_cold = predicted_temperature_60m_c is not None and predicted_temperature_60m_c < comfort_temperature_c - 0.3
        weak_cooling_threshold = -0.15 - min(0.2, 0.05 * max(0, active_cooling_zone_count - 1))
        cooling_too_weak = temperature_trend_c_per_h is not None and temperature_trend_c_per_h > weak_cooling_threshold
        cooling_too_strong = temperature_trend_c_per_h is not None and temperature_trend_c_per_h < -0.35
        learned_cooling_weak = (
            thermal_profile is not None
            and thermal_profile.cooling_trend_c_per_h is not None
            and thermal_profile.cooling_trend_c_per_h > weak_cooling_threshold
        )
        exposed_to_sun = (
            direct_sun
            and irradiance_w_m2 is not None
            and irradiance_w_m2 >= 250
            and (shade_open_percent is None or shade_open_percent > 10)
        )
        shaded_and_stable = (
            direct_sun
            and shade_open_percent is not None
            and shade_open_percent <= 10
            and thermal_profile is not None
            and thermal_profile.passive_shaded_trend_c_per_h is not None
            and thermal_profile.passive_shaded_trend_c_per_h <= 0.1
        )

        if above_comfort and forecast_too_warm and (cooling_too_weak or learned_cooling_weak):
            target -= 1.0
            factors.append("Sollwertwirkung zu schwach")
        elif above_comfort and exposed_to_sun and cooling_too_weak:
            target -= 1.0
            factors.append("offene Beschattung und Solarertrag")
        elif below_comfort and (forecast_too_cold or cooling_too_strong):
            target += 1.0
            factors.append("Kühlwirkung zu stark")
        elif temperature_c <= comfort_temperature_c and shaded_and_stable and temperature_trend_c_per_h is not None and temperature_trend_c_per_h <= 0:
            target += 1.0
            factors.append("geschützte Fassade ohne Wiederaufheizung")
        elif target_offset_c < 0 and (temperature_c <= comfort_temperature_c + 0.2 or (temperature_trend_c_per_h is not None and temperature_trend_c_per_h <= -0.25)):
            target += 1.0
            factors.append("Kühlwirkung wieder ausreichend")
        elif target_offset_c > 0 and (temperature_c >= comfort_temperature_c - 0.2 or (temperature_trend_c_per_h is not None and temperature_trend_c_per_h >= -0.1)):
            target -= 1.0
            factors.append("Kühlbedarf wieder gestiegen")
        elif target_offset_c:
            factors.append("gelernte Modellkorrektur")

        if factors and active_cooling_zone_count > 1:
            factors.append(f"{active_cooling_zone_count} Innengeräte am gemeinsamen Außengerät")
        target = min(max_target_c, max(min_target_c, target))
        return target, tuple(factors), target - base_target_c

    @staticmethod
    def _relieved_room_target(
        dynamic_target_c: float,
        temperature_c: float,
        comfort_temperature_c: float,
        hold_target_c: float,
        max_target_c: float,
    ) -> float:
        """Reduce demand by one step without abandoning an overheated room."""
        if temperature_c <= comfort_temperature_c + 0.25:
            return max_target_c
        return min(max_target_c, hold_target_c, dynamic_target_c + 1.0)

    def export_runtime_state(self) -> dict[str, object]:
        """Persist explicit ownership and the bounded learned target correction.

        The observed device snapshot is retained so the first post-restart
        refresh can still distinguish the handed-over state from a manual
        change. Timers deliberately remain process-local and start fresh.
        """
        return {
            "owns_cooling": self._owns_cooling,
            "manual_override_active": self._manual_override_active,
            "model_target_offset_c": self._model_target_offset_c,
            "observed_snapshot": None if self._observed_snapshot is None else list(self._observed_snapshot),
        }

    def restore_runtime_state(self, state: object) -> None:
        """Restore a prior explicit handover; malformed data fails closed."""
        if not isinstance(state, dict):
            return
        self._manual_override_active = state.get("manual_override_active") is True
        offset = state.get("model_target_offset_c")
        if isinstance(offset, (int, float)):
            self._model_target_offset_c = min(5.0, max(-5.0, float(offset)))
        if state.get("owns_cooling") is not True:
            return
        snapshot = state.get("observed_snapshot")
        if not isinstance(snapshot, list) or len(snapshot) != 4 or not all(value is None or isinstance(value, (str, float, int)) for value in snapshot):
            return
        self._owns_cooling = True
        self._observed_snapshot = (snapshot[0], snapshot[1], snapshot[2], snapshot[3])
        self._expected_snapshot = None

    def mark_sent(self, action: PilotAction) -> None:
        """Record only a command accepted by the guarded write boundary."""
        now = self._clock()
        pending_model_offset = self._pending_model_target_offset_c
        self._pending_model_target_offset_c = None
        if action.action == "start":
            self._owns_cooling = True
            self._cooling_started_at = now
            self._active_target_temperature_c = action.target_temperature_c
            self._last_target_change_at = now
            self._expected_snapshot_at = now
            self._pv_missing_since = None
            self._settled_since = None
            self._overcooling_since = None
            self._expected_snapshot = ("cool", action.target_temperature_c, None if self._observed_snapshot is None else self._observed_snapshot[2], None if self._observed_snapshot is None else self._observed_snapshot[3])
        elif action.action == "adjust":
            self._active_target_temperature_c = action.target_temperature_c
            self._last_target_change_at = now
            self._expected_snapshot_at = now
            self._expected_snapshot = ("cool", action.target_temperature_c, None if self._observed_snapshot is None else self._observed_snapshot[2], None if self._observed_snapshot is None else self._observed_snapshot[3])
            if action.reason_code == "thermal_relief_adjustment":
                self._thermal_relief_since = now
                self._overcooling_since = None
            if action.reason_code in {"heat_pump_priority_relief_step", "heat_pump_priority_recovery_step"}:
                self._last_heat_pump_relief_at = now
            if action.reason_code == "pilot_model_feedback_adjustment" and pending_model_offset is not None:
                self._model_target_offset_c = pending_model_offset
        elif action.action == "stop":
            self.release_ownership()
            self._demand_since = None
            self._last_stopped_at = now
            self._sunset_takeover_active = False

    def decide(
        self,
        config: ControllerConfig,
        *,
        temperature_c: float | None,
        climate_mode: str | None,
        granted_stages: int,
        export_power_w: float | None,
        outdoor_unit_power_w: float | None = None,
        heat_pump_priority_active: bool = False,
        heat_pump_power_w: float | None = None,
        heat_pump_relief_step_interval_s: float = 60.0,
        thermal_profile: ThermalProfile | None = None,
        direct_sun: bool = False,
        irradiance_w_m2: float | None = None,
        temperature_trend_c_per_h: float | None = None,
        predicted_temperature_60m_c: float | None = None,
        shade_open_percent: float | None = None,
        active_cooling_zone_count: int = 1,
        climate_target_temperature_c: float | None = None,
        climate_fan_mode: str | None = None,
        climate_swing_mode: str | None = None,
        pv_deadline_active: bool = False,
        manual_change_candidate: bool = True,
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
        if climate_mode != "cool":
            # A stopped unit ends the evening handover.  The next cooling run
            # starts with normal manual ownership until the next deadline.
            self._sunset_takeover_active = False
        if pv_deadline_active:
            self._sunset_takeover_active = True
            self._manual_override_active = False
        if self._owns_cooling and manual_change_candidate and self._manual_change_detected(snapshot):
            self.release_ownership()
            self._manual_override_active = True
            if not self._sunset_takeover_active:
                return PilotAction("none", None, "manual_control_resumed", f"Manuelle Änderung erkannt; {self._display_name}-Pilot hat die Kontrolle zurückgegeben.")
        pv_available = export_power_w is not None and export_power_w >= config.min_pv_surplus_w
        hard_limit = temperature_c >= zone.hard_max_temperature
        # The configured comfort value is the thermal promise, even when the
        # indoor unit accepts whole degrees only.  A 23.5 °C comfort target is
        # therefore held at 24 °C: starting at 23 °C makes this inverter cool
        # unnecessarily hard and produces an avoidable 23/24 °C saw-tooth.
        # Deeper pre-cooling remains a separate, deliberately rare strong-PV
        # decision below.
        min_target = self._min_start_target_c if zone.pilot_min_target_temperature is None else zone.pilot_min_target_temperature
        max_target = self._thermal_relief_target_c if zone.pilot_max_target_temperature is None else zone.pilot_max_target_temperature
        max_target = max(min_target, max_target)
        hold_target = min(max(min_target, max_target - 1.0), max(min_target, float(ceil(zone.comfort_temperature))))
        living_room_band = self._expected_zone_name.casefold() == "wohnzimmer"
        cool_target = (
            hold_target
            if living_room_band
            else max(min_target, hold_target - 1.0)
            if abs(zone.comfort_temperature - round(zone.comfort_temperature)) > 0.01
            else hold_target
        )
        deep_precool_target = min_target
        dynamic_room_target = self._dynamic_room_target(
            temperature_c,
            zone.comfort_temperature,
            zone.hard_max_temperature,
            min_target,
            max_target,
        )
        pv_capacity_target = self._pv_capacity_target(
            export_power_w=export_power_w,
            minimum_surplus_w=config.min_pv_surplus_w,
            outdoor_unit_power_w=outdoor_unit_power_w,
            min_target_c=min_target,
            max_target_c=max_target,
        )
        strong_pv = export_power_w is not None and export_power_w >= 2 * config.min_pv_surplus_w
        needs_cooling = hard_limit or (pv_available and temperature_c > zone.comfort_temperature)

        if climate_mode == "cool" and not self._owns_cooling:
            if self._manual_override_active and not self._sunset_takeover_active:
                return PilotAction("none", None, "manual_control_resumed", f"Manuelle Änderung ist aktiv; {self._display_name}-Pilot wartet bis zur nächsten Übergabe oder PV-Abendregelung.")
            # A running room is manual by default. Only a dashboard handover
            # or the defined PV-evening deadline transfers it to the pilot.
            # Persisted pilot ownership already survives an HA restart.
            if not self._takeover_requested and not self._sunset_takeover_active:
                self._manual_override_active = True
                return PilotAction("none", None, "manual_control_resumed", f"{self._display_name} läuft manuell; Pilot greift erst nach Übergabe ein.")
            self._adopt_external_cooling(now, snapshot)
        if not self._owns_cooling:
            if not needs_cooling:
                self._demand_since = None
                return PilotAction("none", None, "pv_or_thermal_need_missing", "Kein PV-gestützter oder thermischer Kühlbedarf.")
            if not hard_limit and self._last_stopped_at is not None and now - self._last_stopped_at < self._MIN_OFF_TIME_S:
                return PilotAction("none", None, "pilot_resting", f"{self._display_name}-Pilot hält die Kompressor-Ruhezeit ein.")
            start_target = dynamic_room_target if living_room_band else cool_target
            if temperature_c > zone.comfort_temperature + 0.25 and pv_capacity_target is not None:
                start_target = min(start_target, pv_capacity_target)
            if hard_limit:
                return PilotAction("start", start_target, "hard_temperature_limit", f"Harte Temperaturgrenze erreicht; Kühlung startet temperaturgeführt bei {start_target:.0f} °C.")
            if self._demand_since is None:
                self._demand_since = now
            if now - self._demand_since < 600:
                return PilotAction("none", None, "pilot_demand_stabilizing", "PV-Kühlbedarf wird zehn Minuten auf Stabilität geprüft.")
            return PilotAction("start", start_target, "pv_preconditioning", f"PV-Überschuss startet eine ruhige, temperaturgeführte Kühlung bei {start_target:.0f} °C.")

        if climate_mode != "cool":
            self.release_ownership()
            return PilotAction("none", None, "pilot_start_unconfirmed", "Pilotstart ist am Klimagerät noch nicht bestätigt.")

        # The Loxone energy manager has granted the heat pump. Grid export is
        # already net of that measured load, so never subtract it a second
        # time. While the compressor is running, however, a bare 100 W export
        # is not enough reserve: require its currently measured draw on top of
        # the configured minimum before allowing normal climate modulation.
        priority_reserve_w = config.min_pv_surplus_w + max(0.0, outdoor_unit_power_w or 0.0)
        priority_inputs_known = export_power_w is not None and outdoor_unit_power_w is not None
        priority_reserve_available = priority_inputs_known and export_power_w >= priority_reserve_w
        if heat_pump_priority_active and not priority_inputs_known and not hard_limit:
            current_target = climate_target_temperature_c if climate_target_temperature_c is not None else self._active_target_temperature_c
            return PilotAction(
                "none",
                None,
                "heat_pump_priority_data_waiting",
                f"Wärmepumpen-Priorität aktiv; {self._display_name} hält den bestehenden Sollwert, bis Netzeinspeisung und Außengeräteleistung gültig sind.",
                current_target,
            )
        if heat_pump_priority_active and not priority_reserve_available and not hard_limit:
            current_target = climate_target_temperature_c if climate_target_temperature_c is not None else self._active_target_temperature_c
            power_text = "unbekannter Leistung" if heat_pump_power_w is None else f"{heat_pump_power_w:.0f} W"
            comfort_target = dynamic_room_target if living_room_band else cool_target
            comfort_relief_cap = min(
                max_target,
                comfort_target + (1.0 if temperature_c > zone.comfort_temperature + 0.25 else 0.0),
            )
            if current_target is None:
                current_target = comfort_target
            current_target = min(max_target, max(min_target, current_target))
            if current_target > comfort_relief_cap:
                guard_target = max(comfort_relief_cap, current_target - 1.0)
                return PilotAction(
                    "adjust",
                    guard_target,
                    "heat_pump_priority_comfort_guard",
                    f"Wärmepumpen-Priorität aktiv ({power_text}), aber {self._display_name} liegt noch über dem Komfortziel; Solltemperatur wird schrittweise von {current_target:.0f} auf {guard_target:.0f} °C zurückgeführt.",
                    guard_target,
                )
            if current_target >= comfort_relief_cap:
                return PilotAction(
                    "none",
                    None,
                    "heat_pump_priority_comfort_holding",
                    f"Wärmepumpen-Priorität aktiv ({power_text}); {self._display_name} hält bei {comfort_relief_cap:.0f} °C, damit das Raum-Komfortziel erreichbar bleibt.",
                    comfort_relief_cap,
                )
            step_interval_s = max(60.0, heat_pump_relief_step_interval_s)
            if self._last_heat_pump_relief_at is not None and now - self._last_heat_pump_relief_at < step_interval_s:
                remaining_s = step_interval_s - (now - self._last_heat_pump_relief_at)
                return PilotAction(
                    "none",
                    None,
                    "heat_pump_priority_step_waiting",
                    f"Wärmepumpen-Priorität aktiv ({power_text}); {self._display_name} hält {current_target:.0f} °C und wartet noch {max(1, ceil(remaining_s / 60))} Minute(n) auf die nächste gestaffelte Stufe.",
                    current_target,
                )
            priority_target = min(comfort_relief_cap, current_target + 1.0)
            return PilotAction(
                "adjust",
                priority_target,
                "heat_pump_priority_relief_step",
                f"Wärmepumpen-Priorität aktiv ({power_text}); {self._display_name} wird in dieser Minutenstufe von {current_target:.0f} auf {priority_target:.0f} °C entlastet.",
                priority_target,
            )

        # A high setpoint is not a guarantee that the indoor unit has stopped
        # removing heat.  Small rooms can continue cooling well below their
        # comfort target from thermal inertia or a slow compressor.  Preserve
        # long PV runs by default, but stop once two independent observations
        # confirm a meaningful overshoot with a still-falling temperature.
        below_comfort = temperature_c <= zone.comfort_temperature - self._overshoot_margin_c
        cooling_fast = temperature_trend_c_per_h is not None and temperature_trend_c_per_h <= self._RAPID_COOLING_C_PER_H
        forecast_below_comfort = (
            predicted_temperature_60m_c is not None
            and predicted_temperature_60m_c <= zone.comfort_temperature - self._overshoot_margin_c
        )
        if (pv_available or hard_limit) and below_comfort and (cooling_fast or forecast_below_comfort):
            if self._thermal_relief_since is not None:
                elapsed = now - self._thermal_relief_since
                if elapsed < self._thermal_relief_observation_s:
                    return PilotAction(
                        "none",
                        None,
                        "thermal_relief_observing",
                        f"{self._display_name} hält bei {max_target:.0f} °C; Pilot beobachtet noch {max(1, ceil((self._thermal_relief_observation_s - elapsed) / 60))} Minute(n), ob die Temperatur stabil wird.",
                    )
                return PilotAction(
                    "stop",
                    None,
                    "thermal_relief_unsuccessful",
                    f"{self._display_name} kühlt auch bei {max_target:.0f} °C weiter unter das Komfortband; das Klimagerät wird zum Schutz vor Überkühlung ausgeschaltet.",
                )
            if self._overcooling_since is None:
                self._overcooling_since = now
                return PilotAction(
                    "none",
                    None,
                    "thermal_overshoot_confirming",
                    f"{self._display_name} liegt bereits unter dem Komfortband und kühlt weiter; Pilot bestätigt den Auslauf {self._overshoot_confirmation_s / 60:g} Minute(n) lang.",
                )
            if now - self._overcooling_since >= self._overshoot_confirmation_s:
                if (self._active_target_temperature_c or min_target) < max_target:
                    return PilotAction(
                        "adjust",
                        max_target,
                        "thermal_relief_adjustment",
                        f"{self._display_name} kühlt weiter unter das Komfortband; Solltemperatur wird zuerst auf {max_target:.0f} °C angehoben und der Raum wird beobachtet.",
                    )
                return PilotAction(
                    "stop",
                    None,
                    "thermal_overshoot_stop",
                    f"{self._display_name} kühlt trotz hohem Sollwert weiter unter das Komfortband; das Klimagerät wird zum Schutz vor Überkühlung ausgeschaltet.",
                )
        else:
            self._overcooling_since = None
            self._thermal_relief_since = None

        runtime_s = 0.0 if self._cooling_started_at is None else now - self._cooling_started_at
        target_change_due = self._last_target_change_at is None or now - self._last_target_change_at >= self._TARGET_CHANGE_INTERVAL_S
        # Every room uses its external sensor as the control input. The living
        # room starts with its proportional full-range target; the smaller and
        # sleeping rooms retain their quieter room-specific baseline. Observed
        # trend, forecast, sun/shade context and shared outdoor-unit load then
        # integrate one corrective degree at each calm control interval.
        desired_target = dynamic_room_target if living_room_band else (cool_target if temperature_c > zone.comfort_temperature else hold_target)
        model_factors: tuple[str, ...] = ()
        proposed_model_offset = self._model_target_offset_c
        if runtime_s >= self._TARGET_CHANGE_INTERVAL_S:
            desired_target, model_factors, proposed_model_offset = self._model_adjusted_target(
                desired_target,
                temperature_c=temperature_c,
                comfort_temperature_c=zone.comfort_temperature,
                min_target_c=min_target,
                max_target_c=max_target,
                temperature_trend_c_per_h=temperature_trend_c_per_h,
                predicted_temperature_60m_c=predicted_temperature_60m_c,
                thermal_profile=thermal_profile,
                direct_sun=direct_sun,
                irradiance_w_m2=irradiance_w_m2,
                shade_open_percent=shade_open_percent,
                active_cooling_zone_count=active_cooling_zone_count,
                target_offset_c=self._model_target_offset_c,
            )
        if not living_room_band and strong_pv and runtime_s >= self._DEEP_PRECOOL_AFTER_S and temperature_c > hold_target + 0.5:
            desired_target = deep_precool_target

        # While the room is still warm, use genuinely available net export to
        # spread the target over the entire configured range.  This makes the
        # inverter absorb surplus PV until the external room sensor reaches
        # comfort, rather than treating PV as a simple on/off permission.
        pv_capacity_active = False
        if temperature_c > zone.comfort_temperature + 0.25 and pv_capacity_target is not None and pv_capacity_target < desired_target:
            desired_target = pv_capacity_target
            pv_capacity_active = True
            target_change_due = target_change_due or (
                self._last_target_change_at is None
                or now - self._last_target_change_at >= self._PV_CAPACITY_TARGET_INTERVAL_S
            )

        # Return from heat-pump relief just as smoothly as we entered it.  A
        # recovered reserve must not leave a warm room parked at the relaxed
        # maximum until the normal 15-minute modulation window expires.
        # Instead, lower exactly one whole device degree per room rotation;
        # the shared command adapter still permits only one house command per
        # minute.
        reported_target = climate_target_temperature_c if climate_target_temperature_c is not None else self._active_target_temperature_c
        recovering_from_heat_pump_relief = (
            self._last_heat_pump_relief_at is not None
            and reported_target is not None
            and reported_target > desired_target
            and (pv_available or hard_limit)
            and (not heat_pump_priority_active or priority_reserve_available)
        )
        if recovering_from_heat_pump_relief:
            step_interval_s = max(60.0, heat_pump_relief_step_interval_s)
            if now - self._last_heat_pump_relief_at < step_interval_s:
                remaining_s = step_interval_s - (now - self._last_heat_pump_relief_at)
                return PilotAction(
                    "none",
                    None,
                    "heat_pump_priority_recovery_waiting",
                    f"Wärmepumpenreserve ist wieder ausreichend; {self._display_name} hält {reported_target:.0f} °C und wartet noch {max(1, ceil(remaining_s / 60))} Minute(n) auf die nächste Rückregelstufe.",
                    reported_target,
                )
            recovery_target = max(desired_target, reported_target - 1.0)
            return PilotAction(
                "adjust",
                recovery_target,
                "heat_pump_priority_recovery_step",
                f"Wärmepumpenreserve ist wieder ausreichend; {self._display_name} wird in dieser Minutenstufe von {reported_target:.0f} auf {recovery_target:.0f} °C zurückgeregelt.",
                recovery_target,
            )
        if self._last_heat_pump_relief_at is not None and reported_target is not None and reported_target <= desired_target:
            self._last_heat_pump_relief_at = None

        if not pv_available and not hard_limit:
            self._settled_since = None
            if self._pv_missing_since is None:
                self._pv_missing_since = now
            # PV loss must not abruptly surrender comfort.  While the
            # authoritative room sensor is still meaningfully above comfort,
            # retain the quiet whole-degree hold target so the inverter can
            # reduce the excess heat continuously.  Only inside the comfort
            # band may the device relax to its upper target and run out.
            # This keeps a short PV dip from turning a warm room into an
            # avoidable 20 -> 25 °C on/off cycle.
            room_above_comfort_band = living_room_band and temperature_c > zone.comfort_temperature + 0.25
            wind_down_target = (
                self._relieved_room_target(dynamic_room_target, temperature_c, zone.comfort_temperature, hold_target, max_target)
                if living_room_band
                else max_target
            )
            current_target = self._active_target_temperature_c
            if current_target is None:
                current_target = climate_target_temperature_c if climate_target_temperature_c is not None else min_target
            if abs(current_target - wind_down_target) > 0.01:
                if room_above_comfort_band:
                    return PilotAction(
                        "adjust",
                        wind_down_target,
                        "pv_comfort_hold",
                        f"PV-Überschuss fehlt, aber {self._display_name} liegt noch über dem Komfortband; die dynamische Solltemperatur wird nur auf {wind_down_target:.0f} °C entlastet.",
                        wind_down_target,
                    )
                return PilotAction("adjust", max_target, "pv_wind_down", f"PV-Überschuss fehlt und der Raum ist im Komfortband; Solltemperatur wird auf {max_target:.0f} °C angehoben. Das Innengerät darf sparsam auslaufen.", max_target)
            if not room_above_comfort_band and outdoor_unit_power_w is not None and outdoor_unit_power_w <= config.no_pv_hold_max_power_w:
                return PilotAction(
                    "none",
                    None,
                    "low_power_hold",
                    f"PV-Überschuss fehlt, aber die Außeneinheit benötigt nur {outdoor_unit_power_w:.0f} W (Grenze {config.no_pv_hold_max_power_w:.0f} W); {self._display_name} hält bei {max_target:.0f} °C bewusst weiter.",
                    max_target,
                )
            if not room_above_comfort_band and now - self._pv_missing_since >= self._PV_WIND_DOWN_S:
                return PilotAction("stop", None, "pv_surplus_ended", "PV-Überschuss bleibt aus; der sparsame Auslauf ist beendet.")
            if room_above_comfort_band:
                return PilotAction(
                    "none",
                    None,
                    "pv_comfort_holding",
                    f"PV-Überschuss fehlt, aber {self._display_name} liegt noch über dem Komfortband; die Kühlung moduliert bei {wind_down_target:.0f} °C ohne Takten weiter.",
                    wind_down_target,
                )
            return PilotAction(
                "none",
                None,
                "pv_wind_down_waiting",
                f"PV-Überschuss fehlt; {self._display_name} läuft bei {max_target:.0f} °C sparsam aus und wird nach dem Auslauf-Fenster ohne PV abgeschaltet.",
                max_target,
            )
        else:
            self._pv_missing_since = None
            # The desired target is only useful when it is also confirmed by
            # the device.  ConnectLife can overwrite or report a different
            # whole-degree target after an earlier command.  Do not wait for
            # the calm 15-minute modulation interval in that case: after the
            # acknowledgement grace period, re-assert the already planned
            # target.  The command boundary still enforces its five-minute
            # per-device safety interval.
            target_drifted = (
                self._active_target_temperature_c == desired_target
                and
                climate_target_temperature_c is not None
                and abs(climate_target_temperature_c - desired_target) > 0.01
            )
            acknowledgement_complete = (
                self._expected_snapshot_at is None
                or now - self._expected_snapshot_at >= self._COMMAND_ACK_GRACE_S
            )
            if target_drifted and acknowledgement_complete:
                return PilotAction(
                    "adjust",
                    desired_target,
                    "pilot_target_drift",
                    f"{self._display_name} meldet {climate_target_temperature_c:.0f} °C statt des geplanten Sollwerts {desired_target:.0f} °C; Pilot stellt den wirksamen Kühl-Sollwert wieder her.",
                )
            if self._active_target_temperature_c != desired_target and target_change_due:
                if pv_capacity_active:
                    return PilotAction(
                        "adjust",
                        desired_target,
                        "pv_capacity_preconditioning",
                        f"PV-Reserve wird genutzt; {self._display_name} regelt für das Raumziel auf {desired_target:.0f} °C vor.",
                    )
                if model_factors:
                    self._pending_model_target_offset_c = proposed_model_offset
                    return PilotAction(
                        "adjust",
                        desired_target,
                        "pilot_model_feedback_adjustment",
                        f"Externer Raumsensor und Raummodell regeln auf {desired_target:.0f} °C nach ({'; '.join(model_factors)}).",
                    )
                return PilotAction("adjust", desired_target, "pilot_soft_target_adjustment", f"Der externe Raumsensor führt die Solltemperatur ruhig auf {desired_target:.0f} °C nach.")

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
            if self._active_target_temperature_c != desired_target and target_change_due:
                self._settled_since = now
                return PilotAction("adjust", desired_target, "pilot_settling", f"Kühlziel erreicht; Solltemperatur wird zum ruhigen Auslaufen auf {desired_target:.0f} °C angehoben.")
            if self._settled_since is None:
                self._settled_since = now
                return PilotAction("none", None, "pilot_settling", "Kühlziel erreicht; Pilot prüft zehn Minuten lang, ob der Raum stabil bleibt.")
            if now - self._settled_since >= self._SETTLE_STOP_DELAY_S:
                return PilotAction("stop", None, "thermal_target_reached", "Kühlziel ist stabil erreicht; ohne erwartete Wiederaufheizung wird das Klimagerät ausgeschaltet.")
        else:
            self._settled_since = None

        if self._sunset_takeover_active:
            return PilotAction("none", None, "sunset_pv_control_active", "PV-Abendregelung ist aktiv; die Kühlung wird bis zum geordneten Auslauf geführt.")
        if model_factors:
            return PilotAction(
                "none",
                None,
                "pilot_model_feedback_holding",
                f"{self._display_name} hält den modellkorrigierten Sollwert {desired_target:.0f} °C ({'; '.join(model_factors)}).",
                desired_target,
            )
        return PilotAction("none", None, "pilot_cooling_active", f"{self._display_name} wird mit PV bei {desired_target:.0f} °C ruhig und langlaufend moduliert.", desired_target)

    def _adopt_external_cooling(self, now: float, snapshot: tuple[str | None, float | None, str | None, str | None]) -> None:
        """Treat an explicitly handed-over cooling run as pilot-owned from now on."""
        self._owns_cooling = True
        self._cooling_started_at = now
        self._active_target_temperature_c = None
        self._last_target_change_at = None
        self._expected_snapshot_at = None
        self._pv_missing_since = None
        self._settled_since = None
        self._overcooling_since = None
        self._thermal_relief_since = None
        self._takeover_requested = False
        self._observed_snapshot = snapshot
        self._expected_snapshot = None

    def _manual_change_detected(self, snapshot: tuple[str | None, float | None, str | None, str | None]) -> bool:
        """Differentiate a real user setpoint/mode change from device churn.

        ConnectLife can refresh fan and swing attributes independently of a
        command to another indoor unit. Those incidental updates must never
        release another room's pilot ownership.
        """
        if self._expected_snapshot is not None:
            if self._control_signature(snapshot) == self._control_signature(self._expected_snapshot):
                self._observed_snapshot = snapshot
                self._expected_snapshot = None
                self._expected_snapshot_at = None
                return False
            if self._expected_snapshot_at is not None and self._clock() - self._expected_snapshot_at < self._COMMAND_ACK_GRACE_S:
                return False
            if self._observed_snapshot is not None and self._control_signature(snapshot) != self._control_signature(self._observed_snapshot):
                return True
            self._observed_snapshot = snapshot
            return False
        if self._observed_snapshot is None:
            self._observed_snapshot = snapshot
            return False
        if self._control_signature(snapshot) == self._control_signature(self._observed_snapshot):
            self._observed_snapshot = snapshot
            return False
        return True

    @staticmethod
    def _control_signature(snapshot: tuple[str | None, float | None, str | None, str | None]) -> tuple[str | None, float | None]:
        """Use only the pilot-owned climate fields for ownership detection."""
        return snapshot[0], snapshot[1]

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

    def _pv_wind_down_step_interval_s(self, export_power_w: float | None, required_surplus_w: float) -> float:
        """Return a slower target-ramp interval when some PV is still available."""
        if export_power_w is None or required_surplus_w <= 0:
            return self._PV_WIND_DOWN_FAST_STEP_S
        retained_ratio = max(0.0, min(1.0, export_power_w / required_surplus_w))
        return self._PV_WIND_DOWN_FAST_STEP_S + (
            self._PV_WIND_DOWN_SLOW_STEP_S - self._PV_WIND_DOWN_FAST_STEP_S
        ) * retained_ratio


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
