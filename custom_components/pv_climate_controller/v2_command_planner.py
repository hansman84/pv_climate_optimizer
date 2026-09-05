"""Turn one approved V2 Shadow candidate into a deliberately mild plan."""

from __future__ import annotations

from math import floor
from time import monotonic
from typing import Callable

from .quiet_fan_control import (
    FAN_AUTO,
    FAN_QUIET,
    FanFeatures,
    FanRuntime,
    FanState,
    evaluate_fan_stage,
    tick_fan_runtime,
)
from .v2_models import CandidateAction, HouseDecision, RoomCandidate, V2CommandPlan, V2RoomInput


class V2CommandPlanner:
    """Plan no more than one safe existing-device target step.

    It does not call Home Assistant and refuses to invent a target when a room
    has no explicit pilot bounds or climate capabilities.
    """

    def __init__(self, now_fn: Callable[[], float] = monotonic) -> None:
        self._now_fn = now_fn
        self._fan_runtimes: dict[str, FanRuntime] = {}

    @staticmethod
    def _measured_room_temp_c(room: V2RoomInput) -> float | None:
        value = room.snapshot.room_temperature.value
        if isinstance(value, (int, float)) and room.snapshot.room_temperature.is_valid:
            return float(value)
        return None

    def _fan_mode(self, room: V2RoomInput, candidate: RoomCandidate, target: float | None) -> str | None:
        """Draft-minimising fan stage for a real target step (quiet first).

        The indoor fan follows the cooling gap: ``low`` by default, stepping
        up only when the gap persists (grace period), one step per interval.
        ``high`` is reserved for hard-limit protection.  Stop/wind-down plans
        never carry a fan command.
        """
        if target is None or not room.supported_fan_modes:
            return None
        measured = self._measured_room_temp_c(room)
        if measured is None:
            return None
        modes = {mode.casefold(): mode for mode in room.supported_fan_modes}
        supported = tuple(
            modes[mode] for mode in (FAN_AUTO, FAN_QUIET, "middle_low", "medium", "middle_high", "high") if mode in modes
        )
        if not supported:
            return None

        target_for_gap = target if target is not None else room.comfort_temperature_c
        gap_c = measured - target_for_gap
        now_s = self._now_fn()
        runtime = self._fan_runtimes.setdefault(room.policy.room_id, FanRuntime())
        runtime, stable_s, changed_s = tick_fan_runtime(runtime, gap_c, now_s)
        self._fan_runtimes[room.policy.room_id] = runtime

        hard = room.hard_max_temperature_c is not None and measured >= room.hard_max_temperature_c
        features = FanFeatures(
            gap_c=gap_c,
            gap_stable_s=stable_s,
            boost_active=candidate.reason_code in {"outdoor_pv_boost", "pv_boosted", "hard_temperature_limit_failsafe"},
            hard_limit_exceeded=hard,
            action_stop=candidate.action is CandidateAction.STOP,
            supported_stages=supported,
        )
        decision = evaluate_fan_stage(features, FanState(current_stage=runtime.current_stage, fan_changed_recently_s=changed_s))
        if decision.stage != runtime.current_stage:
            self._fan_runtimes[room.policy.room_id] = FanRuntime(
                current_stage=decision.stage, last_change_at_s=now_s, band_since_at_s=runtime.band_since_at_s
            )
        observed = room.observed_fan_mode
        if decision.stage == FAN_AUTO:
            return None if observed in {None, FAN_AUTO} else modes.get(FAN_AUTO)
        selected = modes.get(decision.stage)
        if selected is None or selected == observed:
            return None
        return selected

    def normalize_fan_plan(self, room: V2RoomInput) -> V2CommandPlan | None:
        """Quiet-fan normalisation for an already-cooling room.

        A room that V2 holds or takes over (external start, no target step
        needed) must still settle on the draft-minimising fan stage.  Emits a
        fan-only adjust plan (same target) at most once per stage interval.
        """
        if room.observed_hvac_mode != "cool" or not room.supported_fan_modes:
            return None
        measured = self._measured_room_temp_c(room)
        target = room.observed_target_temperature_c
        if measured is None or target is None:
            return None
        modes = {mode.casefold(): mode for mode in room.supported_fan_modes}
        supported = tuple(
            modes[mode] for mode in (FAN_AUTO, FAN_QUIET, "middle_low", "medium", "middle_high", "high") if mode in modes
        )
        if not supported:
            return None
        gap_c = measured - target
        now_s = self._now_fn()
        runtime = self._fan_runtimes.setdefault(room.policy.room_id, FanRuntime())
        runtime, stable_s, changed_s = tick_fan_runtime(runtime, gap_c, now_s)
        self._fan_runtimes[room.policy.room_id] = runtime
        hard = room.hard_max_temperature_c is not None and measured >= room.hard_max_temperature_c
        features = FanFeatures(
            gap_c=gap_c, gap_stable_s=stable_s, boost_active=False,
            hard_limit_exceeded=hard, action_stop=False, supported_stages=supported,
        )
        decision = evaluate_fan_stage(features, FanState(current_stage=runtime.current_stage, fan_changed_recently_s=changed_s))
        observed = room.observed_fan_mode
        if decision.stage == FAN_AUTO:
            return None if observed in {None, FAN_AUTO} else None
        selected = modes.get(decision.stage)
        if selected is None or selected == observed:
            return None
        if decision.stage != runtime.current_stage:
            self._fan_runtimes[room.policy.room_id] = FanRuntime(
                current_stage=decision.stage, last_change_at_s=now_s, band_since_at_s=runtime.band_since_at_s
            )
        return V2CommandPlan(
            room.policy.room_id, CandidateAction.ADJUST, target, "v2_fan_normalize",
            "V2 normalisiert den Lüfter auf die zugluftarme Stufe (gleicher Sollwert).", selected,
        )

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
            if room.evening_comfort_active and candidate.target_after_c is None and lower is not None:
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
