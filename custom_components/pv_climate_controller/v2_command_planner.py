"""Turn one approved V2 Shadow candidate into a deliberately mild plan."""

from __future__ import annotations

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
        if lower is None or upper is None or step is None:
            return None
        if room.observed_hvac_mode != "cool":
            return V2CommandPlan(
                room.policy.room_id,
                CandidateAction.START,
                upper,
                "v2_gentle_start",
                "V2 startet mit dem mildesten explizit erlaubten Pilotsollwert.",
            )
        current = room.observed_target_temperature_c
        if current is None:
            return None
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
