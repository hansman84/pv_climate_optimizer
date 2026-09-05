"""Pure, draft-minimising fan-stage selector for V2 comfort cooling.

Design intent (2026-09-05, household request: draft sensitivity during
active cooling):

* Cooling capacity comes from the compressor/target first (the existing
  -1 K boost step); the indoor fan stays light.
* The fan stage follows the *cooling gap* (room temperature - effective
  target).  As long as the room is coming down the fan stays at ``low``.
* Only when the gap persists above a threshold for a grace period does the
  stage step up - one level at a time, at most one step per interval.
* Step down immediately when the gap closes (hysteresis baked into the
  raise thresholds).  ``high`` is reserved for hard-limit protection.

Deterministic, no IO, never raises.  Temperatures in °C, times in s.
"""

from __future__ import annotations

from dataclasses import dataclass

FAN_AUTO = "auto"
FAN_QUIET = "low"
FAN_STEP = "middle_low"
FAN_MEDIUM = "medium"
FAN_HIGH = "high"

FAN_ORDER: tuple[str, ...] = (FAN_QUIET, FAN_STEP, FAN_MEDIUM, "middle_high", FAN_HIGH)

STEP_UP_GAP_C = 1.5
STEP_UP_HARD_GAP_C = 2.5
STEP_UP_GRACE_S = 20 * 60.0
STEP_DOWN_HYSTERESIS_C = 0.5
STEP_INTERVAL_S = 5 * 60.0

# Boost capacity comes from the compressor; never exceed this stage then.
BOOST_MAX_INDEX = 1  # middle_low


@dataclass(frozen=True, slots=True)
class FanFeatures:
    """One thermal snapshot for one zone."""

    gap_c: float
    gap_stable_s: float
    boost_active: bool = False
    hard_limit_exceeded: bool = False
    action_stop: bool = False
    target_at_capacity_floor: bool = False  # setpoint already ~1 K below comfort (max compressor)
    supported_stages: tuple[str, ...] = (FAN_AUTO, FAN_QUIET, FAN_STEP, FAN_MEDIUM, "middle_high", FAN_HIGH)


@dataclass(frozen=True, slots=True)
class FanState:
    """Persistent regulator state for one zone (kept by the caller)."""

    current_stage: str = FAN_QUIET
    fan_changed_recently_s: float = 0.0  # seconds since the last stage change


@dataclass(frozen=True, slots=True)
class FanDecision:
    """The fan stage the controller should command now."""

    stage: str
    reason_code: str
    reason_text: str


@dataclass(frozen=True, slots=True)
class FanRuntime:
    """Per-zone runtime kept by the caller across ticks (monotonic seconds)."""

    current_stage: str = FAN_QUIET
    last_change_at_s: float = -1e9   # a large negative value => "never changed yet"
    band_since_at_s: float | None = None  # when the current gap band started


def tick_fan_runtime(runtime: FanRuntime, gap_c: float, now_s: float) -> tuple[FanRuntime, float, float]:
    """Advance the gap-band bookkeeping; return (runtime, gap_stable_s, changed_ago_s)."""
    band_since = runtime.band_since_at_s
    if gap_c >= STEP_UP_GAP_C:
        if band_since is None:
            band_since = now_s
        stable_s = max(0.0, now_s - band_since)
    else:
        band_since = None
        stable_s = 0.0
    updated = FanRuntime(
        current_stage=runtime.current_stage,
        last_change_at_s=runtime.last_change_at_s,
        band_since_at_s=band_since,
    )
    return updated, stable_s, max(0.0, now_s - runtime.last_change_at_s)


def _clamp_to_supported(supported: tuple[str, ...], wanted_index: int) -> str:
    """Return the highest supported stage at or below the wanted index."""
    for index in range(wanted_index, -1, -1):
        candidate = FAN_ORDER[index]
        if candidate in supported:
            return candidate
    return FAN_AUTO if FAN_AUTO in supported else FAN_ORDER[0]


def evaluate_fan_stage(features: FanFeatures, state: FanState) -> FanDecision:
    """Pick the next fan stage for one zone snapshot (deterministic)."""

    supported = tuple(features.supported_stages)
    current = state.current_stage if state.current_stage in supported else FAN_AUTO
    current_index = FAN_ORDER.index(current) if current in FAN_ORDER else 0

    # 1. Stop / wind-down: leave the fan alone.
    if features.action_stop:
        return FanDecision(FAN_AUTO, "stop_no_fan_cmd", "Kein Lüfter-Eingriff beim Stopp/Auslauf.")

    # 2. Hard limit: full capacity, highest stage.
    if features.hard_limit_exceeded:
        stage = _clamp_to_supported(supported, FAN_ORDER.index(FAN_HIGH))
        return FanDecision(stage, "hard_limit_capacity", "Harte Grenze überschritten: maximale Lüfterstufe.")

    # 3. Boost: compressor does the work, airflow stays light.
    if features.boost_active:
        desired = min(current_index, BOOST_MAX_INDEX)
        stage = _clamp_to_supported(supported, desired)
        if stage != current:
            return FanDecision(stage, "boost_light_air", "Boost über Kompressor: Lüfter auf leichter Stufe gehalten.")
        return FanDecision(stage, "boost_keep", "Boost aktiv: leichte Stufe beibehalten.")

    # 4. Comfort regulation: desired stage from the gap (with grace period).
    if features.gap_c >= STEP_UP_HARD_GAP_C and features.gap_stable_s >= STEP_UP_GRACE_S:
        desired_index = FAN_ORDER.index(FAN_MEDIUM)
        reason = ("gap_large_persistent", "Gap ≥ 2,5 K über 20 min: mittlere Stufe (temporär).")
    elif features.gap_c >= STEP_UP_GAP_C and features.gap_stable_s >= STEP_UP_GRACE_S:
        desired_index = FAN_ORDER.index(FAN_STEP)
        reason = ("gap_persistent", "Gap ≥ 1,5 K über 20 min: eine Stufe höher.")
    else:
        desired_index = 0
        reason = ("gap_small", "Raum im Griff: leichte Lüfterstufe.")

    # 4a. Capacity gate: raise the fan only after the setpoint already uses the
    # compressor at its comfort floor.  While the target can still go lower,
    # extra capacity must come from the compressor (quieter), not the fan.
    if desired_index > 0 and not features.target_at_capacity_floor:
        desired_index = 0
        reason = ("capacity_via_target", "Mehr Kälteleistung über den Sollwert (Kompressor), Lüfter bleibt leise.")

    # 5. Step discipline: at most one step up per interval; step down freely.
    if desired_index > current_index:
        if state.fan_changed_recently_s < STEP_INTERVAL_S:
            desired_index = current_index  # wait for the interval to elapse
            reason = ("step_interval", "Stufenwechsel-Intervall (5 min) noch nicht abgelaufen.")
        elif desired_index > current_index + 1:
            desired_index = current_index + 1
            reason = ("step_once", "Nur eine Stufe pro Intervall.")
    elif desired_index < current_index and state.fan_changed_recently_s < 60.0:
        # Allow a fast step-down only after a short guard; otherwise keep
        # current stage so brief sensor noise cannot bounce the fan.
        if current_index - desired_index == 1:
            pass  # one-step down is fine even after a short time
        else:
            desired_index = current_index

    stage = _clamp_to_supported(supported, desired_index)
    return FanDecision(stage, reason[0], reason[1])
