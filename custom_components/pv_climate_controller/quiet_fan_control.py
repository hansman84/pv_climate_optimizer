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

# Fine air-speed ladder (0.7.0).  When a room is being held on a level (a
# "hold" setpoint below comfort), airflow is the *fine* actuator: it changes the
# delivered cooling in small increments without touching the setpoint, the
# compressor keeps modulating instead of switching off, and the room stops
# saw-toothing.  One stage per FINE_STEP_WIDTH_C above the level, observed for
FINE_STEP_GAP_C = 0.4
FINE_STEP_WIDTH_C = 0.55
FINE_STABLE_S = 3 * 60.0
FINE_STEP_DOWN_C = 0.4

# Boost capacity comes from the compressor; never exceed this stage then.
BOOST_MAX_INDEX = 1  # middle_low

# 0.11.0 Tradeoff Temperatur <-> Geblaese (Hauswunsch 2026-09-21 "finde einen
# guten Tradeoff zwischen Temperatur und Geblaesestaerken"):
#   1. Im Normalbetrieb wird nie lauter als *medium* gefahren - darueber
#      entsteht Zug und Laerm.  Lautere Stufen gibt es nur im Notfall
#      (harte Grenze / Dead-End), der oben separat behandelt wird.
#   2. Der Luefter geht nur dann eine Stufe hoeher, wenn der Raum wirklich nicht
#      folgt: Abweichung >= 0.4 K, mindestens 20 Minuten anhaltend, UND die
#      Temperatur faellt nicht (Trend > -0.15 K/h).  Mehr Kaelteleistung kommt
#      sonst zuerst ueber den Sollwert (Kompressor) - der ist zugfrei.
NORMAL_MAX_STAGE_INDEX = 2  # medium
CAPACITY_BOOST_GAP_C = 0.4
CAPACITY_BOOST_TREND_C_PER_H = -0.15
CAPACITY_BOOST_AFTER_S = 20 * 60.0
# 0.13.0 (Hauswunsch 2026-09-21: "es kann ruhig auch auf hoechster Stufe laufen,
# wenn es notwendig ist - aber im Wohnzimmer soll Zug vermieden werden, also
# erst als letztes Mittel"):  Bleibt der Raum trotz mittlerer Stufe stehen, darf
# der Luefter nach laengerer Zeit weiter hoch - eine Stufe alle 5 Minuten.
ESCALATE_MIDDLE_HIGH_GAP_C = 1.0
ESCALATE_MIDDLE_HIGH_AFTER_S = 35 * 60.0
ESCALATE_HIGH_GAP_C = 1.5
ESCALATE_HIGH_AFTER_S = 50 * 60.0


def stall_ceiling_index(gap_c: float, stable_s: float) -> int:
    """Hoechste erlaubte Luefterstufe, wenn der Raum nicht folgt.

    Standard ist *medium* (zugarm).  Erst wenn Abweichung UND Zeit zusammen
    gross sind, geht es eine Stufe hoeher - letztes Mittel, nicht erstes.
    """
    if gap_c >= ESCALATE_HIGH_GAP_C and stable_s >= ESCALATE_HIGH_AFTER_S:
        return FAN_ORDER.index(FAN_HIGH)
    if gap_c >= ESCALATE_MIDDLE_HIGH_GAP_C and stable_s >= ESCALATE_MIDDLE_HIGH_AFTER_S:
        return FAN_ORDER.index("middle_high")
    return NORMAL_MAX_STAGE_INDEX


@dataclass(frozen=True, slots=True)
class FanFeatures:
    """One thermal snapshot for one zone."""

    gap_c: float
    gap_stable_s: float
    boost_active: bool = False
    hard_limit_exceeded: bool = False
    action_stop: bool = False
    target_at_capacity_floor: bool = False  # setpoint already ~1 K below comfort (max compressor)
    # 0.7.0: the room is being held on a level, so the fan may step finely with
    # the gap instead of waiting for the old 1.5 K / 20 minute ladder.
    fine_ladder: bool = False
    # 0.11.0: der gemessene Trend des Regelwerts.  Der Luefter geht nur eine
    # Stufe hoeher, wenn die Temperatur trotz Abweichung nicht folgt.
    pull_down_c_per_h: float | None = None
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


def tick_fan_runtime(
    runtime: FanRuntime, gap_c: float, now_s: float, threshold_c: float = STEP_UP_GAP_C
) -> tuple[FanRuntime, float, float]:
    """Advance the gap-band bookkeeping; return (runtime, gap_stable_s, changed_ago_s)."""
    band_since = runtime.band_since_at_s
    if gap_c >= threshold_c:
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

    # 4. Fine ladder (0.7.0): the room is held on a level, so airflow modulates
    # with the gap - one stage every FINE_STEP_WIDTH_C above the level, after a
    # short confirmation, one step per interval.  This is what keeps a glazed
    # room on its level instead of cycling the compressor on and off.
    if features.fine_ladder:
        # 0.11.0 Tradeoff: solange die Temperatur folgt, bleibt der Luefter
        # leise (die Kaelte kommt aus dem Sollwert/Kompressor, das ist zugfrei).
        # Erst wenn der Raum NICHT folgt, darf der Luefter nachfuehren - eine
        # Stufe, gedeckelt auf medium.
        follows = (
            features.pull_down_c_per_h is not None
            and features.pull_down_c_per_h <= CAPACITY_BOOST_TREND_C_PER_H
        )
        stalled = (
            features.pull_down_c_per_h is not None
            and features.pull_down_c_per_h > CAPACITY_BOOST_TREND_C_PER_H
        )
        if stalled and features.gap_c >= CAPACITY_BOOST_GAP_C and features.gap_stable_s >= CAPACITY_BOOST_AFTER_S:
            desired_index = min(NORMAL_MAX_STAGE_INDEX, current_index + 1)
            reason = (
                "capacity_boost",
                (
                    f"Temperatur folgt nicht ({features.pull_down_c_per_h:+.2f} K/h bei "
                    f"{features.gap_c:.1f} K Abweichung): Luefter eine Stufe hoeher."
                ),
            )
        elif follows and features.gap_c >= FINE_STEP_GAP_C:
            desired_index = 0
            reason = (
                "fan_quiet_following",
                (
                    f"Temperatur faellt ausreichend ({features.pull_down_c_per_h:+.2f} K/h) - "
                    "Kaelte kommt ueber den Sollwert, Luefter bleibt leise."
                ),
            )
        elif features.gap_c >= FINE_STEP_GAP_C and features.gap_stable_s >= FINE_STABLE_S:
            steps = 1 + int((features.gap_c - FINE_STEP_GAP_C) // FINE_STEP_WIDTH_C)
            desired_index = min(FAN_ORDER.index(FAN_HIGH), steps)
            desired_index = min(desired_index, stall_ceiling_index(features.gap_c, features.gap_stable_s))
            reason = ("gap_fine_step", f"Feinregelung: {features.gap_c:.1f} K über dem Pegel – Lüfter fein nachgeführt.")
        else:
            desired_index = 0
            reason = ("gap_small", "Raum auf Pegel: zugluftarme Stufe.")
        if desired_index > current_index:
            if state.fan_changed_recently_s < STEP_INTERVAL_S:
                desired_index = current_index
                reason = ("step_interval", "Stufenwechsel-Intervall (5 min) noch nicht abgelaufen.")
            elif desired_index > current_index + 1:
                desired_index = current_index + 1
                reason = ("step_once", "Nur eine Stufe pro Intervall.")
        elif desired_index < current_index:
            # Cool down gently: never drop more than one stage at a time.
            desired_index = max(desired_index, current_index - 1)
        stage = _clamp_to_supported(supported, desired_index)
        return FanDecision(stage, reason[0], reason[1])

    # 4b. Comfort regulation: desired stage from the gap (with grace period).
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

    # 4a2. 0.11.0/0.13.0: im Normalbetrieb zuerst leise (medium); hoehere Stufen
    # erst, wenn der Raum trotz Zeit und Abweichung nicht folgt (letztes Mittel).
    desired_index = min(desired_index, stall_ceiling_index(features.gap_c, features.gap_stable_s))

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
