"""Turn one approved V2 Shadow candidate into a deliberately mild plan."""

from __future__ import annotations

from math import floor

from .v2_models import CandidateAction, HouseDecision, RoomCandidate, V2CommandPlan, V2RoomInput


class V2CommandPlanner:
    """Plan no more than one safe existing-device target step.

    It does not call Home Assistant and refuses to invent a target when a room
    has no explicit pilot bounds or climate capabilities.
    """

    def plan(self, room: V2RoomInput, candidate: RoomCandidate, decision: HouseDecision) -> V2CommandPlan | None:
        if room.policy.room_id not in decision.approved_room_ids:
            return None
        if not candidate.requests_modulation:
            return None
        lower = room.pilot_min_target_temperature_c
        upper = room.pilot_max_target_temperature_c
        step = room.target_temperature_step_c
        if candidate.action is CandidateAction.STOP:
            return V2CommandPlan(
                room.policy.room_id,
                CandidateAction.STOP,
                None,
                "v2_comfort_stop",
                "V2 beendet die Kühlung nach bestätigter Komfortreserve.",
            )
        if lower is None or step is None:
            return None
        if room.observed_hvac_mode != "cool":
            # Older room records may not yet have an explicit pilot ceiling.
            # A failsafe start may still use the last target confirmed by that
            # exact device; it does not invent a new setpoint.
            start_target = upper if upper is not None else room.observed_target_temperature_c
            if room.evening_comfort_active and lower is not None:
                # Evening comfort is a real temperature promise, not a
                # permission to start at the relaxed 25 C ceiling.
                start_target = max(lower, min(upper if upper is not None else lower, floor(room.comfort_temperature_c)))
            if start_target is None:
                return None
            return V2CommandPlan(
                room.policy.room_id,
                CandidateAction.START,
                start_target,
                "v2_gentle_start",
                "V2 startet mit dem mildesten explizit erlaubten oder zuletzt bestätigten Gerätesollwert.",
            )
        if upper is None:
            return None
        current = room.observed_target_temperature_c
        if current is None:
            return None
        if candidate.reason_code == "forecast_comfort_recovered":
            target = min(upper, current + step)
            if target <= current:
                return None
            return V2CommandPlan(
                room.policy.room_id,
                CandidateAction.ADJUST,
                round(target, 3),
                "v2_single_relief_step",
                "V2 hebt den Sollwert nur um eine bestätigte Gerätestufe an und beobachtet danach erneut.",
            )
        target = max(lower, min(upper, current - step))
        if target >= current:
            return None
        return V2CommandPlan(
            room.policy.room_id,
            CandidateAction.ADJUST,
            round(target, 3),
            "v2_single_gentle_step",
            "V2 senkt den Sollwert nur um eine bestätigte Gerätestufe und beobachtet danach erneut.",
        )
