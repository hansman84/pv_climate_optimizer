"""Pure, single-authority V2 house coordinator.

The coordinator allocates the *available* PV headroom across all actionable
rooms.  It deliberately does not issue commands; the shared command adapter
still serializes the approved plans safely at the device boundary.
"""

from __future__ import annotations

from .v2_models import CandidateAction, DecisionState, HouseDecision, RoomCandidate, RoomDecision


class HouseCoordinator:
    """Allocate available house capacity to several calm room steps.

    Normal steps are ordered by configured room precedence, then forecast
    comfort gap and confidence.  Unlike the former one-step ramp, a fitting
    lower-priority room may consume capacity left after a higher-priority room.
    A safety override is never held back by a momentary PV deficit: preventing
    a hard temperature breach and honoring evening comfort is allowed to use
    grid power and remains visible as such in the decision reason.
    """

    def decide(self, candidates: tuple[RoomCandidate, ...], *, available_budget_w: float) -> HouseDecision:
        """Return an explainable budget decision for every supplied room."""
        if available_budget_w < 0:
            raise ValueError("available_budget_w cannot be negative")
        if len({candidate.policy.room_id for candidate in candidates}) != len(candidates):
            raise ValueError("each room may have one V2 candidate per cycle")

        decisions: dict[str, RoomDecision] = {}
        pending = [candidate for candidate in candidates if candidate.requests_modulation]
        for candidate in candidates:
            if not candidate.requests_modulation:
                decisions[candidate.policy.room_id] = RoomDecision(
                    candidate.policy.room_id,
                    DecisionState.NOT_REQUESTED,
                    candidate.reason_code,
                    candidate.reason_text,
                    next_review_at=candidate.next_review_at,
                )

        remaining_budget_w = available_budget_w
        approved: list[str] = []
        for candidate in sorted(pending, key=self._allocation_key):
            if candidate.safety_override:
                approved.append(candidate.policy.room_id)
                decisions[candidate.policy.room_id] = RoomDecision(
                    candidate.policy.room_id,
                    DecisionState.APPROVED_STEP,
                    candidate.reason_code,
                    candidate.reason_text,
                    candidate.action,
                    candidate.next_review_at,
                )
                # Do not pretend a safety action is PV-funded when it exceeds
                # the measured headroom.  The exposed reserved budget remains
                # a real PV allocation, while the reason flags the override.
                remaining_budget_w = max(0.0, remaining_budget_w - candidate.required_budget_w)
                continue
            if candidate.required_budget_w <= remaining_budget_w:
                approved.append(candidate.policy.room_id)
                remaining_budget_w -= candidate.required_budget_w
                decisions[candidate.policy.room_id] = RoomDecision(
                    candidate.policy.room_id,
                    DecisionState.APPROVED_STEP,
                    candidate.reason_code,
                    candidate.reason_text,
                    candidate.action,
                    candidate.next_review_at,
                )
                continue
            decisions[candidate.policy.room_id] = self._budget_block(candidate)

        return HouseDecision(
            tuple(decisions[candidate.policy.room_id] for candidate in candidates),
            tuple(approved),
            available_budget_w - remaining_budget_w,
            available_budget_w,
        )

    @staticmethod
    def _allocation_key(candidate: RoomCandidate) -> tuple[int, int, float, float, str]:
        """Sort safety first, then room role and thermal urgency."""
        return (
            0 if candidate.safety_override else 1,
            candidate.policy.modulation_priority,
            -candidate.comfort_gap_c,
            -candidate.confidence,
            candidate.policy.room_id,
        )

    @staticmethod
    def _budget_block(candidate: RoomCandidate) -> RoomDecision:
        state = DecisionState.COMFORT_RISK_ALERT if candidate.comfort_gap_c > 0 else DecisionState.BLOCKED_WITH_ESCALATION
        return RoomDecision(
            candidate.policy.room_id,
            state,
            "room_budget_unavailable",
            f"{candidate.policy.display_name} benötigt {candidate.required_budget_w:.0f} W, aber nach den höher priorisierten Hausstufen reicht der aktuelle PV-Überschuss nicht aus; Neubewertung ist eingeplant.",
            next_review_at=candidate.next_review_at,
        )
