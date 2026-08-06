"""Pure V2 Shadow Mode candidate construction.

There is intentionally no executor import here.  Missing safety inputs and
unknown power estimates result in an explainable HOLD, never a guessed start.
"""

from __future__ import annotations

from .v2_models import (
    CandidateAction,
    DecisionState,
    HouseDecision,
    RoomCandidate,
    RoomDecision,
    V2RoomInput,
)
from .v2_orchestrator import HouseCoordinator


class V2ShadowRunner:
    """Build candidates then apply the one-step house coordinator."""

    def __init__(self, coordinator: HouseCoordinator | None = None) -> None:
        self._coordinator = coordinator or HouseCoordinator()

    def evaluate(self, rooms: tuple[V2RoomInput, ...], *, available_budget_w: float) -> tuple[tuple[RoomCandidate, ...], HouseDecision]:
        candidates = tuple(self._candidate(room) for room in rooms)
        decision = self._coordinator.decide(candidates, available_budget_w=available_budget_w)
        return candidates, decision

    @staticmethod
    def _candidate(room: V2RoomInput) -> RoomCandidate:
        if room.eligibility.reason_code in {"bedroom_schedule_pending", "bedroom_quiet_time"}:
            if room.observed_hvac_mode == "cool":
                return RoomCandidate(
                    policy=room.policy,
                    action=CandidateAction.STOP,
                    required_budget_w=0.0,
                    comfort_gap_c=0.0,
                    confidence=room.estimate.confidence,
                    reason_code=room.eligibility.reason_code,
                    reason_text=room.eligibility.reason_text,
                    safety_override=True,
                )
            return V2ShadowRunner._hold(room, room.eligibility.reason_code, room.eligibility.reason_text)
        if not room.snapshot.critical_inputs_valid:
            return V2ShadowRunner._hold(room, "critical_input_not_fresh", "V2 wartet: mindestens eine kritische Quelle ist fehlend, unplausibel oder veraltet.")
        if not room.eligibility.allowed:
            return V2ShadowRunner._hold(room, room.eligibility.reason_code, room.eligibility.reason_text)
        temperature = room.estimate.temperature_c
        if (
            temperature is not None
            and temperature >= room.hard_max_temperature_c
            and room.observed_hvac_mode != "cool"
        ):
            return RoomCandidate(
                policy=room.policy,
                action=CandidateAction.START,
                required_budget_w=0.0,
                comfort_gap_c=max(0.0, temperature - room.comfort_temperature_c),
                confidence=room.estimate.confidence,
                reason_code="hard_temperature_limit_failsafe",
                reason_text="V2 Fail-safe: die harte Raumtemperaturgrenze ist erreicht; das Klimagerät wird mit einem bestätigten, milden Sollwert gestartet.",
                safety_override=True,
            )
        if room.required_budget_w is None:
            return V2ShadowRunner._hold(room, "budget_estimate_missing", "V2 wartet: für diesen Raum gibt es noch keine belastbare Leistungsabschätzung.")
        # V1's essential wind-down rule: without export, do not leave an
        # already comfortable room running merely because its old device
        # setpoint is still low.  Evening comfort is the deliberate exception
        # and hard limits remain fail-safe above.
        no_pv = room.snapshot.pv_export_w.is_valid and float(room.snapshot.pv_export_w.value or 0.0) <= 0.0
        if (
            room.observed_hvac_mode == "cool"
            and no_pv
            and not room.evening_comfort_active
            and temperature is not None
            and temperature < room.hard_max_temperature_c
        ):
            return RoomCandidate(
                policy=room.policy, action=CandidateAction.STOP, required_budget_w=0.0,
                comfort_gap_c=0.0, confidence=room.estimate.confidence,
                reason_code="pv_lost_comfort_reached",
                reason_text="V2 übernimmt V1-Auslauf: ohne PV wird unterhalb der harten Komfortgrenze ausgeschaltet; erst die harte Grenze darf wieder einen Start erzwingen.",
                safety_override=True,
            )
        scheduled = room.scheduled_target_temperature_c
        if scheduled is not None and room.observed_hvac_mode == "cool" and room.observed_target_temperature_c is not None:
            if abs(room.observed_target_temperature_c - scheduled) >= (room.target_temperature_step_c or 1.0) - 0.001:
                direction = "entspannt" if scheduled > room.observed_target_temperature_c else "verstärkt"
                return RoomCandidate(
                    policy=room.policy,
                    action=CandidateAction.ADJUST,
                    required_budget_w=0.0,
                    comfort_gap_c=max(0.0, (room.estimate.predicted_temperature_60m_c or room.comfort_temperature_c) - room.comfort_temperature_c),
                    confidence=room.estimate.confidence,
                    reason_code="scheduled_comfort_trajectory",
                    reason_text=f"V2 folgt dem berechneten Schlafraum-Verlauf und {direction} nur um eine Gerätestufe.",
                    target_before_c=room.observed_target_temperature_c,
                    target_after_c=scheduled,
                )
        if (
            room.observed_hvac_mode == "cool"
            and room.observed_target_temperature_c is not None
            and room.pilot_min_target_temperature_c is not None
            and room.observed_target_temperature_c <= room.pilot_min_target_temperature_c
            and (
                room.estimate.predicted_temperature_60m_c is None
                or room.estimate.predicted_temperature_60m_c > room.comfort_temperature_c
            )
        ):
            return V2ShadowRunner._hold(
                room,
                "pilot_target_floor_reached",
                "V2 beobachtet weiter: das Klimagerät läuft bereits auf dem niedrigsten erlaubten Pilotsollwert.",
            )
        predicted = room.estimate.predicted_temperature_60m_c
        if predicted is None or room.estimate.confidence <= 0.0:
            return V2ShadowRunner._hold(room, "forecast_insufficient", "V2 wartet: Temperaturprognose oder Konfidenz reicht noch nicht für eine Modulationsstufe.")
        comfort_gap = predicted - room.comfort_temperature_c
        if comfort_gap <= 0:
            if room.observed_hvac_mode == "cool":
                current = room.observed_target_temperature_c
                upper = room.pilot_max_target_temperature_c
                if current is not None and upper is not None and current < upper:
                    return RoomCandidate(
                        policy=room.policy,
                        action=CandidateAction.ADJUST,
                        required_budget_w=0.0,
                        comfort_gap_c=abs(comfort_gap),
                        confidence=room.estimate.confidence,
                        reason_code="forecast_comfort_recovered",
                        reason_text="V2 entspannt die Kühlung um genau eine bestätigte Gerätestufe, weil die Prognose wieder im Komfortband liegt.",
                    )
                if (
                    current is not None
                    and upper is not None
                    and current >= upper
                    and room.estimate.temperature_c is not None
                    and room.estimate.temperature_c <= room.comfort_temperature_c - 0.5
                ):
                    return RoomCandidate(
                        policy=room.policy,
                        action=CandidateAction.STOP,
                        required_budget_w=0.0,
                        comfort_gap_c=abs(comfort_gap),
                        confidence=room.estimate.confidence,
                        reason_code="comfort_stable_at_relief_target",
                        reason_text="V2 beendet die Kühlung erst nach der sanften Entspannung und ausreichender Komfortreserve.",
                    )
            return V2ShadowRunner._hold(room, "comfort_holding", "V2 beobachtet weiter: die Komfortgrenze wird innerhalb von 60 Minuten nicht überschritten.")
        evening_comfort = room.evening_comfort_active
        return RoomCandidate(
            policy=room.policy,
            action=CandidateAction.ADJUST,
            # An occupied evening promise and a hard limit may use the
            # available house capacity even when momentary export is zero.
            # They are still single, rate-limited device steps.
            required_budget_w=0.0 if evening_comfort else room.required_budget_w,
            comfort_gap_c=comfort_gap,
            confidence=room.estimate.confidence,
            reason_code="evening_comfort_required" if evening_comfort else "forecast_comfort_risk",
            reason_text=(
                "V2 Abendkomfort: der Raum wird trotz fehlendem PV-Export zur vereinbarten Komforttemperatur geführt."
                if evening_comfort
                else "V2 Shadow: Prognose zeigt eine vermeidbare Komfortüberschreitung; eine sanfte Stufe wird angefragt."
            ),
            safety_override=evening_comfort,
            target_after_c=scheduled,
        )

    @staticmethod
    def _hold(room: V2RoomInput, reason_code: str, reason_text: str) -> RoomCandidate:
        return RoomCandidate(
            policy=room.policy,
            action=CandidateAction.HOLD,
            required_budget_w=0.0,
            comfort_gap_c=0.0,
            confidence=room.estimate.confidence,
            reason_code=reason_code,
            reason_text=reason_text,
        )
