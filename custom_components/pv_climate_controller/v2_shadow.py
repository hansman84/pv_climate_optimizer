"""Pure V2 Shadow Mode candidate construction.

There is intentionally no executor import here.  Missing safety inputs and
unknown power estimates result in an explainable HOLD, never a guessed start.
"""

from __future__ import annotations

from time import monotonic

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

    _PV_WIND_DOWN_S = 30 * 60
    _EVENING_WIND_DOWN_S = 10 * 60
    _EVENING_IRRADIANCE_W_M2 = 50.0

    def __init__(self, coordinator: HouseCoordinator | None = None, *, clock=monotonic) -> None:
        self._coordinator = coordinator or HouseCoordinator()
        self._clock = clock
        self._pv_missing_since: dict[str, float] = {}

    def evaluate(self, rooms: tuple[V2RoomInput, ...], *, available_budget_w: float) -> tuple[tuple[RoomCandidate, ...], HouseDecision]:
        candidates = tuple(self._candidate(room) for room in rooms)
        decision = self._coordinator.decide(candidates, available_budget_w=available_budget_w)
        return candidates, decision

    def _candidate(self, room: V2RoomInput) -> RoomCandidate:
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
        # V1's essential wind-down rule: without export, do not leave an
        # already comfortable room running merely because its old device
        # setpoint is still low.  Evening comfort is the deliberate exception
        # and hard limits remain fail-safe above.
        # Energy telemetry that is absent or stale must never keep an already
        # running room cooling indefinitely.  It blocks new starts elsewhere;
        # here it triggers the same graceful V1 wind-down path as measured
        # zero export.
        # A few watts are meter noise, not usable compressor capacity.  Using
        # ``> 0`` here let tiny evening/cloud export readings reset the
        # wind-down clock indefinitely, while a V1 start correctly requires
        # the configured minimum reserve.  Apply that same threshold to an
        # already-running room, so the house has one definition of usable PV.
        pv_available = (
            room.snapshot.pv_export_w.is_valid
            and float(room.snapshot.pv_export_w.value or 0.0) >= room.pv_surplus_threshold_w
        )
        now = self._clock()
        if pv_available:
            self._pv_missing_since.pop(room.policy.room_id, None)
        else:
            self._pv_missing_since.setdefault(room.policy.room_id, now)
        no_pv_for_s = 0.0 if pv_available else now - self._pv_missing_since[room.policy.room_id]
        wind_down_s = (
            self._EVENING_WIND_DOWN_S
            if room.solar_irradiance_w_m2 is not None and room.solar_irradiance_w_m2 <= self._EVENING_IRRADIANCE_W_M2
            else self._PV_WIND_DOWN_S
        )
        if (
            room.observed_hvac_mode == "cool"
            and not pv_available
            and temperature is not None
            and temperature < room.hard_max_temperature_c
            # A sleeping-room deadline is a comfort promise.  Once it is at
            # risk, do not oscillate between no-PV stop and a deadline start;
            # the trajectory branch below owns the device until the forecast
            # is safe again or the configured hard cutoff arrives.
            and not room.deadline_at_risk
        ):
            upper = room.pilot_max_target_temperature_c
            target = room.observed_target_temperature_c
            still_needs_evening_comfort = room.evening_comfort_active and temperature > room.comfort_temperature_c + 0.25
            if not still_needs_evening_comfort and upper is not None and target is not None and target < upper:
                return RoomCandidate(
                    policy=room.policy, action=CandidateAction.ADJUST, required_budget_w=0.0,
                    comfort_gap_c=max(0.0, temperature - room.comfort_temperature_c), confidence=room.estimate.confidence,
                    reason_code="pv_wind_down", reason_text="V2 übernimmt V1-Auslauf: ohne PV wird der Gerätesollwert sofort auf die sparsame Auslaufstufe angehoben.",
                    safety_override=True, target_before_c=target, target_after_c=upper,
                )
            if not still_needs_evening_comfort and no_pv_for_s >= wind_down_s:
                return RoomCandidate(
                    policy=room.policy, action=CandidateAction.STOP, required_budget_w=0.0,
                    comfort_gap_c=0.0, confidence=room.estimate.confidence,
                    reason_code="pv_surplus_ended", reason_text=f"V2-Auslauf: PV-Überschuss bleibt seit {int(wind_down_s // 60)} Minuten aus; die Kühlung wird beendet.",
                    safety_override=True,
                )
            return RoomCandidate(
                policy=room.policy, action=CandidateAction.HOLD, required_budget_w=0.0,
                comfort_gap_c=max(0.0, temperature - room.comfort_temperature_c), confidence=room.estimate.confidence,
                reason_code="pv_wind_down_waiting",
                reason_text=f"V2-Auslauf: ohne PV läuft das Gerät nur auf der entspannten Stufe aus und wird nach {int(wind_down_s // 60)} Minuten abgeschaltet.",
            )
        # A learned incremental demand is required only to *start* a new
        # compressor load.  It must never veto a no-PV wind-down or stop of
        # an already running, comfortable room above.
        if room.required_budget_w is None:
            return V2ShadowRunner._hold(room, "budget_estimate_missing", "V2 wartet: für diesen Raum gibt es noch keine belastbare Leistungsabschätzung.")
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
        living_room_priority = room.policy.display_name.strip().casefold() == "wohnzimmer" and comfort_gap >= 0.4
        deadline_priority = room.deadline_at_risk
        evening_target = None
        if evening_comfort and room.observed_hvac_mode == "cool":
            # An old PV-precool target (for example 20 C) must never leak
            # into occupied evening use.  V1 immediately hands the device to
            # its evening target; V2 carries that target explicitly so the
            # planner can make the same non-aggressive transition.
            evening_target = room.comfort_temperature_c
        return RoomCandidate(
            policy=room.policy,
            action=CandidateAction.ADJUST,
            # An occupied evening promise and a hard limit may use the
            # available house capacity even when momentary export is zero.
            # They are still single, rate-limited device steps.
            required_budget_w=0.0 if evening_comfort else room.required_budget_w,
            comfort_gap_c=comfort_gap,
            confidence=room.estimate.confidence,
            reason_code=("evening_comfort_required" if evening_comfort else "sleep_deadline_risk" if deadline_priority else "living_room_comfort_priority" if living_room_priority else "forecast_comfort_risk"),
            reason_text=(
                "V2 Abendkomfort: der Raum wird trotz fehlendem PV-Export zur vereinbarten Komforttemperatur geführt."
                if evening_comfort
                else "V2 Shadow: Prognose zeigt eine vermeidbare Komfortüberschreitung; eine sanfte Stufe wird angefragt."
            ),
            safety_override=evening_comfort or living_room_priority or deadline_priority,
            target_after_c=evening_target if evening_target is not None else scheduled,
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
