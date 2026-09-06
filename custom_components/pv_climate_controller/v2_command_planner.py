"""Turn one approved V2 Shadow candidate into a deliberately mild plan."""

from __future__ import annotations

from math import floor
from time import monotonic
from typing import Callable

from .quiet_fan_control import (
    FAN_AUTO,
    FAN_QUIET,
    STEP_UP_GAP_C,
    STEP_UP_GRACE_S,
    STEP_UP_HARD_GAP_C,
    FanFeatures,
    FanRuntime,
    FanState,
    evaluate_fan_stage,
    tick_fan_runtime,
)
from .v2_models import CandidateAction, HouseDecision, RoomCandidate, V2CommandPlan, V2RoomInput

SETTLE_STOP_RESERVE_C = 0.6    # stop once the room is this far below comfort
SETTLE_TARGET_TOL_C = 0.1      # allowed setpoint deviation from comfort
CAPACITY_FLOOR_DELTA_C = 1.0   # compressor floor: comfort - 1 K before the fan may step up


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
        if not getattr(room, "quiet_fan_active", True):
            # Sleep/child rooms: the device's automatic fan modulation is
            # allowed; only restore auto if a manual stage is still active.
            if FAN_AUTO in room.supported_fan_modes and room.observed_fan_mode not in {None, FAN_AUTO}:
                return FAN_AUTO
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
            target_at_capacity_floor=hard or (target is not None and target <= room.comfort_temperature_c - CAPACITY_FLOOR_DELTA_C),
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

    def settle_plan(self, room: V2RoomInput) -> V2CommandPlan | None:
        """Idle convergence for a running room under V2 authority.

        Ensures the comfort target is reached without draft and without
        overcooling: raise an over-eager setpoint up to comfort, lower a too
        warm setpoint towards comfort while the room is still above it, stop
        as soon as the room is comfortably below comfort, and otherwise keep
        the fan on the quiet stage.  One gentle step per tick.
        """
        if room.observed_hvac_mode != "cool":
            return None
        measured = self._measured_room_temp_c(room)
        target = room.observed_target_temperature_c
        step = room.target_temperature_step_c or 1.0
        comfort = room.comfort_temperature_c
        modes = {mode.casefold(): mode for mode in room.supported_fan_modes}
        supported = tuple(
            modes[mode] for mode in (FAN_AUTO, FAN_QUIET, "middle_low", "medium", "middle_high", "high") if mode in modes
        )
        quiet = modes.get(FAN_QUIET) if modes.get(FAN_QUIET) else None

        def _fan_plan(new_target: float, reason_code: str, reason_text: str) -> V2CommandPlan:
            fan = quiet if quiet and quiet != room.observed_fan_mode else None
            return V2CommandPlan(room.policy.room_id, CandidateAction.ADJUST, round(new_target, 2), reason_code, reason_text, fan)

        if target is None:
            return None
        stop_reserve_c = 0.4 if room.occupied_window_active else SETTLE_STOP_RESERVE_C
        if measured is not None and measured <= comfort - stop_reserve_c:
            # Comfort reached: stop instead of holding the room cold.
            return V2CommandPlan(room.policy.room_id, CandidateAction.STOP, None, "v2_comfort_reached",
                                 f"V2 beendet die Kühlung: Raum ({measured:.1f} °C) liegt {stop_reserve_c:.1f} K unter dem Komfortziel {comfort:.1f} °C – kein kaltes Halten, keine Zugluft.", None)
        if target < comfort - SETTLE_TARGET_TOL_C and measured is not None and measured < comfort + 0.5:
            # Over-eager setpoint: raise gently to comfort (less cold, less draft).
            new_target = min(comfort, target + step)
            if new_target > target:
                return _fan_plan(new_target, "v2_comfort_converge_up",
                                 f"V2 hebt den Sollwert Richtung Komfort {comfort:.1f} °C an (kein Überkühlen, sanftere Modulation).")
        if target > comfort + SETTLE_TARGET_TOL_C and measured is not None and measured > comfort + 0.5:
            # Room still above comfort but the setpoint is warmer than the goal.
            new_target = max(comfort, target - step)
            if new_target < target:
                return _fan_plan(new_target, "v2_comfort_converge_down",
                                 f"V2 senkt den Sollwert auf das Komfortziel {comfort:.1f} °C, damit der Raum es erreicht.")

        # ---- Capacity guard: comfort must stay reachable. ----
        if measured is None:
            return None
        now_s = self._now_fn()
        runtime = self._fan_runtimes.setdefault(room.policy.room_id, FanRuntime())
        runtime, stable_s, _changed_s = tick_fan_runtime(runtime, measured - comfort, now_s)
        self._fan_runtimes[room.policy.room_id] = runtime
        floor_target = comfort - CAPACITY_FLOOR_DELTA_C
        if room.pilot_min_target_temperature_c is not None:
            floor_target = max(floor_target, room.pilot_min_target_temperature_c)
        at_floor = target <= floor_target + SETTLE_TARGET_TOL_C
        if (not at_floor) and (measured - comfort) >= STEP_UP_GAP_C and stable_s >= STEP_UP_GRACE_S:
            # Compressor first: one setpoint step towards the comfort floor
            # (fan stays on the quiet stage).
            new_target = max(floor_target, target - step)
            if new_target < target:
                return _fan_plan(new_target, "v2_capacity_target_boost",
                                 f"V2 erhöht die Kälteleistung über den Sollwert ({new_target:.1f} °C, Kompressor) – der Lüfter bleibt leise.")
            at_floor = True
        if at_floor and (measured - comfort) >= STEP_UP_HARD_GAP_C and stable_s >= STEP_UP_GRACE_S and getattr(room, "quiet_fan_active", True):
            # Last resort: setpoint already at the comfort floor, still too warm.
            fan_step = modes.get("middle_low")
            if fan_step is not None and fan_step != room.observed_fan_mode:
                return V2CommandPlan(room.policy.room_id, CandidateAction.ADJUST, target, "v2_capacity_fan_boost",
                                     "V2 hebt den Lüfter eine Stufe an (Sollwert bereits am Komfort-Boden, Leistung reicht sonst nicht).", fan_step)
        if (not at_floor) and stable_s < STEP_UP_GRACE_S and (measured - comfort) >= STEP_UP_GAP_C:
            return None  # grace period: observe before boosting capacity

        # Target is at comfort (or in its grace window): only the fan may need settling.
        if not getattr(room, "quiet_fan_active", True):
            return None  # automatic fan modulation is allowed for this room
        if quiet is None or quiet == room.observed_fan_mode:
            return None
        if measured <= comfort + 0.5:
            return V2CommandPlan(room.policy.room_id, CandidateAction.ADJUST, target, "v2_fan_normalize",
                                 "V2 normalisiert den Lüfter auf die zugluftarme Stufe (gleicher Sollwert).", quiet)
        return None

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
