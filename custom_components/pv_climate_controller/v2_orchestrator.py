"""Pure, single-authority V2 house coordinator.

This module has no Home Assistant dependency and cannot issue climate commands.
It only decides which room may receive the next *single* modulation step.
"""

from __future__ import annotations

from .v2_models import CandidateAction, DecisionState, HouseDecision, RoomCandidate, RoomDecision


class HouseCoordinator:
    """Allocate one calm modulation step with explicit room precedence.

    Priority is deliberately simple: a smaller configured priority number wins.
    A lower-priority room cannot consume capacity while a higher-priority room
    has a valid pending modulation request.  Only a declared hard safety
    override outranks normal room precedence.
    """

    def decide(self, candidates: tuple[RoomCandidate, ...], *, available_budget_w: float) -> HouseDecision:
        """Return one explainable decision for every supplied room candidate."""
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
        if pending:
            cohort = self._next_priority_cohort(pending)
            candidate = max(
                cohort,
                key=lambda item: (item.comfort_gap_c, item.confidence, item.policy.room_id),
            )
            pending.remove(candidate)
            if candidate.required_budget_w <= remaining_budget_w:
                decisions[candidate.policy.room_id] = RoomDecision(
                    candidate.policy.room_id,
                    DecisionState.APPROVED_STEP,
                    candidate.reason_code,
                    candidate.reason_text,
                    candidate.action,
                    candidate.next_review_at,
                )
                approved.append(candidate.policy.room_id)
                remaining_budget_w -= candidate.required_budget_w
                self._defer_lower_priority(pending, candidate, decisions)
            else:
                decisions[candidate.policy.room_id] = self._budget_block(candidate)
                self._reserve_for_priority(candidate, pending, decisions)

        for candidate in pending:
            if candidate.policy.room_id in decisions:
                continue
            decisions[candidate.policy.room_id] = RoomDecision(
                candidate.policy.room_id,
                DecisionState.WAITING_FOR_OBSERVATION,
                "higher_priority_step_active",
                f"{candidate.policy.display_name} wartet auf die begruendete Modulationsstufe eines priorisierten Raums.",
                next_review_at=candidate.next_review_at,
            )

        return HouseDecision(
            tuple(decisions[candidate.policy.room_id] for candidate in candidates),
            tuple(approved),
            available_budget_w - remaining_budget_w,
            available_budget_w,
        )

    @staticmethod
    def _next_priority_cohort(pending: list[RoomCandidate]) -> list[RoomCandidate]:
        """Select safety overrides first, otherwise the configured priority band."""
        safety = [candidate for candidate in pending if candidate.safety_override]
        if safety:
            return safety
        priority = min(candidate.policy.modulation_priority for candidate in pending)
        return [candidate for candidate in pending if candidate.policy.modulation_priority == priority]

    @staticmethod
    def _budget_block(candidate: RoomCandidate) -> RoomDecision:
        state = DecisionState.COMFORT_RISK_ALERT if candidate.comfort_gap_c > 0 else DecisionState.BLOCKED_WITH_ESCALATION
        return RoomDecision(
            candidate.policy.room_id,
            state,
            "priority_room_budget_unavailable",
            f"{candidate.policy.display_name} hat Vorrang, aber das erforderliche Hausbudget ist nicht verfuegbar; Neubewertung ist eingeplant.",
            next_review_at=candidate.next_review_at,
        )

    @staticmethod
    def _reserve_for_priority(
        priority_candidate: RoomCandidate,
        pending: list[RoomCandidate],
        decisions: dict[str, RoomDecision],
    ) -> None:
        """Keep lower-priority rooms from taking budget reserved for the leader."""
        for candidate in pending:
            if candidate.safety_override:
                continue
            decisions[candidate.policy.room_id] = RoomDecision(
                candidate.policy.room_id,
                DecisionState.BLOCKED_WITH_ESCALATION,
                "budget_reserved_for_priority_room",
                f"Hausbudget bleibt fuer {priority_candidate.policy.display_name} mit hoeherer Modulationsprioritaet reserviert.",
                next_review_at=candidate.next_review_at,
            )

    @staticmethod
    def _defer_lower_priority(
        pending: list[RoomCandidate],
        approved_candidate: RoomCandidate,
        decisions: dict[str, RoomDecision],
    ) -> None:
        """Make the one-step house ramp and its priority effect visible."""
        for candidate in pending:
            if candidate.safety_override:
                continue
            decisions[candidate.policy.room_id] = RoomDecision(
                candidate.policy.room_id,
                DecisionState.WAITING_FOR_OBSERVATION,
                "higher_priority_step_active",
                f"{candidate.policy.display_name} wartet, waehrend {approved_candidate.policy.display_name} die priorisierte Modulationsstufe beobachtet.",
                next_review_at=candidate.next_review_at,
            )
