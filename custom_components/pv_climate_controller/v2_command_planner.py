"""Turn one approved V2 Shadow candidate into a deliberately mild plan."""

from __future__ import annotations

from math import floor

from .v2_models import CandidateAction, HouseDecision, RoomCandidate, V2CommandPlan, V2RoomInput


class V2CommandPlanner:
    """Plan no more than one safe existing-device target step.

    It does not call Home Assistant and refuses to invent a target when a room
    has no explicit pilot bounds or climate capabilities.
    """

    @staticmethod
    def _fan_mode(room: V2RoomInput, candidate: RoomCandidate, target: float | None) -> str | None:
        """Always restore automatic fan control alongside a real target step.

        Compressor target is the sole modulation axis.  V2 must never create
        an audible high/low fan intervention; Auto lets the indoor unit choose
        the quietest viable airflow and keeps dashboard plans truthful.
        """
        if target is None or not room.supported_fan_modes:
            return None
        modes = {mode.casefold(): mode for mode in room.supported_fan_modes}
        selected = modes.get("auto")
        return None if selected is None or selected == room.observed_fan_mode else selected

    def _plan(self, room: V2RoomInput, candidate: RoomCandidate, action: CandidateAction, target: float | None, reason_code: str, reason_text: str) -> V2CommandPlan:
        return V2CommandPlan(room.policy.room_id, action, target, reason_code, reason_text, self._fan_mode(room, candidate, target))

    def plan(self, room: V2RoomInput, candidate: RoomCandidate, decision: HouseDecision) -> V2CommandPlan | None:
        if room.policy.room_id not in decision.approved_room_ids:
            return None
        if not candidate.requests_modulation:
            return None
        lower = room.pilot_min_target_temperature_c
        upper = room.pilot_max_target_temperature_c
        step = room.target_temperature_step_c
        if candidate.action is CandidateAction.STOP:
            return self._plan(room, candidate, CandidateAction.STOP, None, "v2_comfort_stop", "V2 beendet die Kühlung nach bestätigter Komfortreserve.")
        if lower is None or step is None:
            return None
        if room.observed_hvac_mode != "cool":
            # Older room records may not yet have an explicit pilot ceiling.
            # A failsafe start may still use the last target confirmed by that
            # exact device; it does not invent a new setpoint.
            start_target = upper if upper is not None else room.observed_target_temperature_c
            if candidate.target_after_c is not None:
                start_target = max(lower, min(upper if upper is not None else candidate.target_after_c, candidate.target_after_c))
            if room.evening_comfort_active and lower is not None:
                # Evening comfort is a real temperature promise, not a
                # permission to start at the relaxed 25 C ceiling.
                start_target = max(lower, min(upper if upper is not None else lower, floor(room.comfort_temperature_c)))
            if start_target is None:
                return None
            return self._plan(room, candidate, CandidateAction.START, start_target, "v2_gentle_start", "V2 startet mit dem mildesten explizit erlaubten oder zuletzt bestätigten Gerätesollwert.")
        if upper is None:
            return None
        current = room.observed_target_temperature_c
        if current is None:
            return None
        desired = candidate.target_after_c
        if desired is not None:
            desired = max(lower, min(upper, desired))
            if desired > current:
                if candidate.reason_code in {"evening_comfort_required", "pv_wind_down"}:
                    target = desired
                    reason_code = "v2_evening_comfort_handover" if candidate.reason_code == "evening_comfort_required" else "v2_pv_wind_down"
                    reason_text = "V2 beendet die PV-Vorkühlung sofort und übernimmt den ruhigen Abend-Komfortsollwert." if candidate.reason_code == "evening_comfort_required" else "V2 hebt ohne PV sofort auf die sparsame Auslaufstufe an."
                else:
                    target = min(desired, current + step)
                    reason_code, reason_text = "v2_scheduled_relief_step", "V2 entspannt entlang des berechneten Zeit- und Komfortverlaufs nur um eine Gerätestufe."
            elif desired < current:
                target = max(desired, current - step)
                reason_code, reason_text = "v2_scheduled_cooling_step", "V2 verstärkt entlang des berechneten Zeit- und Komfortverlaufs nur um eine Gerätestufe."
            else:
                return None
            return self._plan(room, candidate, CandidateAction.ADJUST, round(target, 3), reason_code, reason_text)
        if candidate.reason_code == "forecast_comfort_recovered":
            target = min(upper, current + step)
            if target <= current:
                return None
            return self._plan(room, candidate, CandidateAction.ADJUST, round(target, 3), "v2_single_relief_step", "V2 hebt den Sollwert nur um eine bestätigte Gerätestufe an und beobachtet danach erneut.")
        target = max(lower, min(upper, current - step))
        if target >= current:
            return None
        return self._plan(room, candidate, CandidateAction.ADJUST, round(target, 3), "v2_single_gentle_step", "V2 senkt den Sollwert nur um eine bestätigte Gerätestufe und beobachtet danach erneut.")
