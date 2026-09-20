"""Pure V2 Shadow Mode candidate construction.

There is intentionally no executor import here.  Missing safety inputs and
unknown power estimates result in an explainable HOLD, never a guessed start.
"""

from __future__ import annotations

from math import floor
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


_DEFAULT_SPLIT_BUDGET_W = 300.0
"""Conservative demand used until a room has its own learned estimate.

Chosen well below a real split's draw (400-1200 W measured on this house) so a
start can never eat the last watts of export by mistake.
"""


class V2ShadowRunner:
    """Build candidates then apply the one-step house coordinator."""

    _PV_WIND_DOWN_S = 30 * 60
    # Evening is occupied time: after PV falls below the meaningful reserve,
    # leave only a short observation window before stopping.  The controller
    # itself reevaluates every minute and a two-minute window filters one
    # transient meter update without letting indoor airflow remain annoying.
    _EVENING_WIND_DOWN_S = 2 * 60
    # Once a unit has acknowledged its relaxed ceiling, keeping it running is
    # no longer a useful anti-cloud-gap measure.  It is effectively fan-only
    # nuisance with no thermal advantage, so stop it promptly.
    _RELAXED_TARGET_STOP_S = 2 * 60
    _EVENING_IRRADIANCE_W_M2 = 50.0
    # The living room is the household's occupied comfort priority.  It uses
    # the same three rules as every other room (comfort, acute, hard limit);
    # the former extra exceptions were removed in 0.5.9.
    _LIVING_NO_PV_COMFORT_GAP_C = 0.4
    # A momentary export spike must not wake a lower-priority compressor only
    # to stop it again with the next cloud sample.  Normal room starts require
    # three continuous minutes of real headroom (household tuning 0.4.55:
    # earlier PV-budget starts); living-room comfort and sleep
    # deadlines remain deliberate, visible exceptions.
    _NORMAL_START_SURPLUS_STABLE_S = 3 * 60
    # Occupied-evening fallback (no per-room presence yet): stop as soon as
    # comfort is reached and restart only after a clearly larger breach, so
    # the evening sofa / bedrooms stay draft-free.  Household tuning 0.4.52:
    # softer band (room feels noticeably warmer with the strict values).
    _OCCUPIED_STOP_RESERVE_C = 0.4
    _OCCUPIED_RESTART_GAP_C = 0.5
    # Short-cycling guard (household report 2026-09-20: "das Wohnzimmer
    # schaltet nervös ein und aus").  A split that has just satisfied its own
    # sensor switches itself off; re-issuing a start two minutes later produced
    # 2-5 minute on/off cycles all morning.  After an observed cool->off
    # transition V2 waits before requesting the next normal start.  Emergencies
    # (hard limit, acute limit) and clearly warm rooms stay exempt.
    _RESTART_COOLDOWN_S = 20 * 60
    _RESTART_EXEMPT_COMFORT_GAP_C = 1.0

    def __init__(self, coordinator: HouseCoordinator | None = None, *, clock=monotonic) -> None:
        self._coordinator = coordinator or HouseCoordinator()
        self._clock = clock
        self._pv_missing_since: dict[str, float] = {}
        self._pv_available_since: dict[str, float] = {}
        self._last_observed_mode: dict[str, str | None] = {}
        self._cooling_off_since: dict[str, float] = {}

    def reset_room_wind_down(self, room_id: str) -> None:
        """Begin a newly adopted room's PV observation window from zero.

        A manual session can outlive the old PV surplus by hours.  Returning
        that still-running unit to V2 must not inherit the old no-PV timer and
        immediately send a stop; V2 first observes it at its relaxed target.
        """
        self._pv_missing_since.pop(room_id, None)
        self._pv_available_since.pop(room_id, None)

    def evaluate(self, rooms: tuple[V2RoomInput, ...], *, available_budget_w: float) -> tuple[tuple[RoomCandidate, ...], HouseDecision]:
        candidates = tuple(self._candidate(room) for room in rooms)
        decision = self._coordinator.decide(candidates, available_budget_w=available_budget_w)
        return candidates, decision

    def _candidate(self, room: V2RoomInput) -> RoomCandidate:
        """Debounce normal restarts after an observed cooling session ended.

        The Hisense indoor unit switches itself off as soon as its own sensor
        reaches the setpoint; V2 then saw an "off" room that was still slightly
        warm and asked for the next start two minutes later.  That produced the
        nervous 2-5 minute on/off pattern reported on 2026-09-20.  Emergency
        paths (hard limit, acute limit) and clearly warm rooms stay exempt.
        """
        now = self._clock()
        room_id = room.policy.room_id
        mode = room.observed_hvac_mode
        previous_mode = self._last_observed_mode.get(room_id)
        self._last_observed_mode[room_id] = mode
        if previous_mode == "cool" and mode != "cool":
            self._cooling_off_since[room_id] = now
        elif mode == "cool":
            self._cooling_off_since.pop(room_id, None)
        candidate = self._candidate_uncapped(room)
        off_since = self._cooling_off_since.get(room_id)
        if (
            off_since is not None
            and now - off_since < self._RESTART_COOLDOWN_S
            and candidate.action in {CandidateAction.START, CandidateAction.ADJUST}
            and mode != "cool"
            and candidate.reason_code not in {"hard_temperature_limit_failsafe", "indoor_acute_need"}
            and candidate.comfort_gap_c < self._RESTART_EXEMPT_COMFORT_GAP_C
        ):
            remaining_min = max(1, int((self._RESTART_COOLDOWN_S - (now - off_since) + 59) // 60))
            return V2ShadowRunner._hold(
                room,
                "restart_cooldown",
                f"V2 wartet {remaining_min} Min. bis zum naechsten Start: das Geraet hat seine Kuehlung gerade selbst beendet (Schutz vor Kurzzyklen).",
            )
        return candidate

    def _candidate_uncapped(self, room: V2RoomInput) -> RoomCandidate:
        # Hard dead-end (household rule): at or above the hard limit the room
        # is cooled against per-room soft rules (outdoor floor, quiet time,
        # PV holds) - but only while cooling is switched on globally.  With
        # the cooling-season switch off (or vacation active) nothing cools.
        cooling_globally_off = (
            room.snapshot.cooling_season_allowed.value is False
            or room.snapshot.vacation_active.value is True
        )
        dead_end_air = room.estimate.temperature_c
        if (
            not cooling_globally_off
            and dead_end_air is not None
            and dead_end_air >= room.hard_max_temperature_c
        ):
            if room.observed_hvac_mode != "cool":
                return RoomCandidate(
                    policy=room.policy,
                    action=CandidateAction.START,
                    required_budget_w=0.0,
                    comfort_gap_c=max(0.0, dead_end_air - room.comfort_temperature_c),
                    confidence=room.estimate.confidence,
                    reason_code="hard_temperature_limit_failsafe",
                    reason_text="V2 Dead-End: harte Temperaturgrenze erreicht - die Kuehlung startet unabhaengig von Saison, Aussengrenze und Ruhezeit.",
                    safety_override=True,
                )
            return RoomCandidate(
                policy=room.policy,
                action=CandidateAction.ADJUST,
                required_budget_w=0.0,
                comfort_gap_c=max(0.0, dead_end_air - room.comfort_temperature_c),
                confidence=room.estimate.confidence,
                reason_code="hard_temperature_limit_failsafe",
                reason_text="V2 Dead-End: harte Temperaturgrenze erreicht - der laufende Betrieb wird gehalten.",
                safety_override=True,
                target_after_c=room.comfort_temperature_c,
            )
        # --- Night quiet time (household decision 2026-09-20) -----------------
        # The end of the Abendkomfort window is also the start of the air
        # conditioner's night quiet time: from then on the living room starts no
        # new cooling at all - neither over the peg nor over the acute limit.
        # Only the hard limit above (and the global cooling-season switch) may
        # still start it, because that is the emergency net.  A unit that is
        # already running is left to the normal rules (comfort stop, PV
        # wind-down); it is never restarted just to hold a level at night.
        if room.night_block_active and room.observed_hvac_mode != "cool":
            return self._hold(
                room,
                "night_quiet_time",
                "V2 Nachtsperre: ab Ende des Abendkomfort-Fensters startet keine Kühlung mehr – nur die harte Temperaturgrenze greift.",
            )
        # --- PV availability bookkeeping (used by the hold mode and by the
        # no-PV wind-down further down) -------------------------------------
        # A few watts are meter noise, not usable compressor capacity, so the
        # house has exactly one definition of usable PV.
        pv_available = (
            room.snapshot.pv_export_w.is_valid
            and float(room.snapshot.pv_export_w.value or 0.0) >= room.pv_surplus_threshold_w
        )
        # A valid 0 W reading is an authoritative no-PV result.
        # 0.5.9 cleanup (Johannes: "WZ-Sonderregeln auf 3 reduzieren"): the
        # living room used to carry three extra exceptions - a missing-inverter
        # "telemetry fallback", a no-export comfort priority and a
        # "Wohnzimmer first" priority step.  They competed for the same
        # setpoint and produced the visible flapping.  The room now uses
        # exactly the same three rules as every other room: comfort target,
        # acute limit, hard limit - with real PV surplus as the precondition.
        predicted = room.estimate.predicted_temperature_60m_c
        usable_cooling_authority = pv_available
        now = self._clock()
        if usable_cooling_authority:
            self._pv_missing_since.pop(room.policy.room_id, None)
        else:
            self._pv_missing_since.setdefault(room.policy.room_id, now)
        if pv_available:
            self._pv_available_since.setdefault(room.policy.room_id, now)
        else:
            self._pv_available_since.pop(room.policy.room_id, None)
        no_pv_for_s = 0.0 if usable_cooling_authority else now - self._pv_missing_since[room.policy.room_id]
        wind_down_s = (
            self._EVENING_WIND_DOWN_S
            if room.solar_irradiance_w_m2 is not None and room.solar_irradiance_w_m2 <= self._EVENING_IRRADIANCE_W_M2
            else self._PV_WIND_DOWN_S
        )
        # --- PV hold mode (household goal 2026-09-20: a steady level instead
        # of saw-toothing, PV only - never grid power).  It sits above the
        # outdoor gate on purpose: the gate is a "no cooling needed" heuristic,
        # while an explicitly configured hold depth is a household wish for a
        # stable temperature.  Hard limit and the acute guard keep priority
        # (they return above), and the hold itself stops below its own band.
        hold_depth = getattr(room, "hold_depth_c", None)
        hold_air = room.estimate.temperature_c
        # The hold itself consumes the surplus it depends on, so the exit
        # threshold must be lower than the entry threshold - otherwise the
        # room cycles: hold eats the export, export drops, hold stops, export
        # returns, hold starts (observed live 2026-09-20).
        hold_surplus_w = (
            float(room.snapshot.pv_export_w.value or 0.0)
            if room.snapshot.pv_export_w.is_valid
            else 0.0
        )
        hold_keep_floor_w = max(30.0, room.pv_surplus_threshold_w * 0.2)
        # "Sparsam, nur mit PV" means the surplus must actually cover what the
        # room's compressor will draw - otherwise the hold silently runs on
        # grid power.  The room's own learned demand is the honest yardstick
        # (fallback: the conservative split estimate).
        hold_demand_w = (
            room.required_budget_w
            if room.required_budget_w is not None
            else _DEFAULT_SPLIT_BUDGET_W
        )
        hold_entry_w = max(room.pv_surplus_threshold_w, 0.8 * float(hold_demand_w))
        hold_keep_floor_w = max(hold_keep_floor_w, 0.25 * hold_entry_w)
        already_cooling = room.observed_hvac_mode == "cool"
        hold_pv_ok = (
            hold_surplus_w >= hold_keep_floor_w
            if already_cooling
            else pv_available
            and hold_surplus_w >= hold_entry_w
            and now - self._pv_available_since.get(room.policy.room_id, now) >= self._NORMAL_START_SURPLUS_STABLE_S
        )
        if (
            hold_depth is not None
            and hold_depth > 0.0
            and room.eligibility.allowed
            and hold_pv_ok
            and hold_air is not None
            # The household's evening window (Abendkomfort) is deliberately
            # relaxed and the night that follows is quiet: inside the window
            # nothing holds a low level, and from its end no new cooling starts
            # at all (household reminder 2026-09-20).  The whole window is used,
            # not just the part where the room is above the evening target.
            and not room.evening_window_active
        ):
            hold_floor = (
                room.pilot_min_target_temperature_c
                if room.pilot_min_target_temperature_c is not None
                else room.comfort_temperature_c - 2.5
            )
            hold_target = max(hold_floor, room.comfort_temperature_c - hold_depth)
            too_cold = hold_air <= room.comfort_temperature_c - hold_depth - 0.5
            # 0.7.2: a real level regulator on the household's own (AirQ) scale.
            # Enter 0.4 K *above* the level (or when the 2 h forecast would
            # exceed it) so the room never drifts up first; stop 0.5 K below.
            # That is a deadband of ~0.9 K around the level instead of waiting
            # until the room is almost at comfort before cooling starts.
            hold_enter_c = room.comfort_temperature_c - hold_depth + 0.4
            needs_hold = hold_air >= hold_enter_c or (
                predicted is not None and predicted >= hold_enter_c
            )
            if not too_cold and needs_hold:
                hold_budget_w = (
                    room.required_budget_w
                    if room.required_budget_w is not None
                    else _DEFAULT_SPLIT_BUDGET_W
                )
                if room.observed_hvac_mode == "cool":
                    current = room.observed_target_temperature_c
                    step = room.target_temperature_step_c or 1.0
                    if current is not None and abs(current - hold_target) >= step - 0.001:
                        return RoomCandidate(
                            policy=room.policy,
                            action=CandidateAction.ADJUST,
                            required_budget_w=0.0,
                            comfort_gap_c=max(0.0, hold_air - room.comfort_temperature_c),
                            confidence=room.estimate.confidence,
                            reason_code="pv_hold_settle",
                            reason_text=(
                                "V2 Pegel halten: das Geraet laeuft mit PV-Ueberschuss auf der "
                                f"Halte-Stufe {hold_target:.1f} C weiter, statt bei Komfort abzuschalten."
                            ),
                            target_before_c=current,
                            target_after_c=hold_target,
                        )
                    return RoomCandidate(
                        policy=room.policy,
                        action=CandidateAction.HOLD,
                        required_budget_w=0.0,
                        comfort_gap_c=max(0.0, hold_air - room.comfort_temperature_c),
                        confidence=room.estimate.confidence,
                        reason_code="pv_hold",
                        reason_text=(
                            f"V2 Pegel halten: {hold_air:.1f} C bei PV-Ueberschuss - die Kuehlung "
                            f"laeuft ruhig auf {hold_target:.1f} C weiter."
                        ),
                    )
                return RoomCandidate(
                    policy=room.policy,
                    action=CandidateAction.START,
                    required_budget_w=hold_budget_w,
                    comfort_gap_c=max(0.0, hold_air - room.comfort_temperature_c),
                    confidence=room.estimate.confidence,
                    reason_code="pv_hold_start",
                    reason_text=(
                        "V2 Pegel halten: PV-Ueberschuss vorhanden - die Kuehlung startet auf die "
                        f"Halte-Stufe {hold_target:.1f} C und laeuft dann ruhig weiter."
                    ),
                    target_after_c=hold_target,
                )
        # The outdoor cooling gate is a transparent, weather-aware pause that
        # lives between the hard failsafe and the bedroom quiet-time handling.
        # The hard failsafe, manual takeover and bedroom rules are unaffected;
        # this only suppresses normal comfort starts while keeping all other
        # V2 reasoning visible.
        gate = getattr(room, "outdoor_cooling_gate", None)
        gate_decision = getattr(gate, "decision", None)
        # Household rule (0.4.60): while the gate says "no cooling needed", a
        # running unit must not keep a low setpoint and cool behind V2's back.
        # Raise it to the relaxed ceiling instead (same idea as the no-PV
        # wind-down, just triggered by the weather gate).
        if gate_decision in {"hold", "rain_hold"}:
            air_now = room.estimate.temperature_c
            acute_for_hold = getattr(room, "acute_cooling_limit_c", None)
            acute_demands_cooling = (
                acute_for_hold is not None and air_now is not None and air_now >= acute_for_hold
            )
            # Household rule (0.5.5): "no cooling needed" plus a room already
            # at/below comfort means the unit must stop - not keep blowing in
            # cool mode on a relaxed setpoint (that is what made the living
            # room feel cold on a mild morning).
            # 0.5.9 cleanup: the gate no longer nudges a running unit onto a
            # "relaxed" ceiling.  That extra step fought the comfort target and
            # was one half of the reported flapping; the gate now either stops
            # a running, comfortable unit or simply reports the hold and lets
            # the room rules decide.
            if (
                not acute_demands_cooling
                and room.observed_hvac_mode == "cool"
                and air_now is not None
                and air_now <= room.comfort_temperature_c + 0.3
            ):
                return RoomCandidate(
                    policy=room.policy,
                    action=CandidateAction.STOP,
                    required_budget_w=0.0,
                    comfort_gap_c=0.0,
                    confidence=room.estimate.confidence,
                    reason_code="v2_comfort_stop",
                    reason_text=(
                        "V2 beendet die Kuehlung: das Gate meldet keinen Bedarf und die Raumluft "
                        "liegt auf oder unter dem Komfortwert."
                    ),
                    safety_override=True,
                )
        if gate is not None and getattr(gate, "decision", None) == "hold":
            return RoomCandidate(
                policy=room.policy,
                action=CandidateAction.HOLD,
                required_budget_w=0.0,
                comfort_gap_c=0.0,
                confidence=room.estimate.confidence,
                reason_code="outdoor_cooling_gate_hold",
                reason_text=f"V2 Outdoor-Cooling-Gate hält: {gate.reason_text}",
                safety_override=False,
            )
        if gate is not None and getattr(gate, "decision", None) == "rain_hold":
            return RoomCandidate(
                policy=room.policy,
                action=CandidateAction.HOLD,
                required_budget_w=0.0,
                comfort_gap_c=0.0,
                confidence=room.estimate.confidence,
                reason_code="outdoor_cooling_gate_rain_hold",
                reason_text=f"V2 Outdoor-Cooling-Gate: {gate.reason_text}",
                safety_override=False,
            )
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
        # Acute cooling guard (household setting): only for rooms that are
        # legitimately eligible right now - season, outdoor floor, bedroom
        # quiet time and fail-safe handling above keep their priority.
        acute_limit = getattr(room, "acute_cooling_limit_c", None)
        acute_air = room.estimate.temperature_c
        if (
            acute_limit is not None
            and acute_air is not None
            and acute_air >= acute_limit
            and room.observed_hvac_mode != "cool"
            and room.eligibility.allowed
        ):
            return RoomCandidate(
                policy=room.policy,
                action=CandidateAction.START,
                required_budget_w=0.0,
                comfort_gap_c=acute_air - room.comfort_temperature_c,
                confidence=room.estimate.confidence,
                reason_code="indoor_acute_need",
                reason_text=(
                    f"V2 Akute Kuehlgrenze ({acute_limit:.1f} C): Raumluft {acute_air:.1f} C - "
                    "Kuehlung wird auch ohne PV-Reserve oder gegen einen Hold freigegeben."
                ),
                safety_override=True,
                target_after_c=room.scheduled_target_temperature_c or room.comfort_temperature_c,
            )
        # Household rule (0.5.3): the acute cooling guard outranks the no-PV
        # wind-down.  While the room air sits at or above the configured acute
        # limit the unit keeps cooling - the family explicitly asked for that
        # even without PV surplus (it is the same rule that allows a start).
        acute_limit = getattr(room, "acute_cooling_limit_c", None)
        acute_air = room.estimate.temperature_c
        if (
            acute_limit is not None
            and acute_air is not None
            and acute_air >= acute_limit
            and room.observed_hvac_mode == "cool"
            and room.eligibility.allowed
        ):
            return RoomCandidate(
                policy=room.policy,
                action=CandidateAction.ADJUST,
                required_budget_w=0.0,
                comfort_gap_c=max(0.0, acute_air - room.comfort_temperature_c),
                confidence=room.estimate.confidence,
                reason_code="indoor_acute_need_hold",
                reason_text=(
                    f"V2 Akute Kuehlgrenze ({acute_limit:.1f} C): Raumluft {acute_air:.1f} C - "
                    "die Kuehlung bleibt auch ohne PV-Ueberschuss aktiv."
                ),
                safety_override=True,
                target_after_c=room.comfort_temperature_c,
            )
        # V1's essential wind-down rule: without export, do not leave an
        # already comfortable room running merely because its old device
        # setpoint is still low.  Evening comfort is the deliberate exception
        # and hard limits remain fail-safe above.
        # Energy telemetry that is absent or stale must never keep an already
        # running room cooling indefinitely.  It blocks new starts elsewhere;
        # here it triggers the same graceful V1 wind-down path as measured
        # zero export.
        if (
            room.observed_hvac_mode == "cool"
            and not usable_cooling_authority
            and temperature is not None
            and temperature < room.hard_max_temperature_c
            # recovers, this branch resumes the normal no-PV wind-down.
            # A sleeping-room deadline is a comfort promise.  Once it is at
            # risk, do not oscillate between no-PV stop and a deadline start;
            # the trajectory branch below owns the device until the forecast
            # is safe again or the configured hard cutoff arrives.
            and not room.deadline_at_risk
            # The living-room evening start is a deadline too.  Do not wind
            # down a calm pre-cool step shortly before occupied comfort.
            and not room.evening_deadline_at_risk
        ):
            upper = room.pilot_max_target_temperature_c
            target = room.observed_target_temperature_c
            still_needs_evening_comfort = room.evening_comfort_active and temperature > room.comfort_temperature_c + 0.25
            if still_needs_evening_comfort:
                # The evening promise keeps a running unit alive: no wind-down,
                # no relaxed ceiling - it stays on until the promised evening
                # temperature is reached.
                return RoomCandidate(
                    policy=room.policy, action=CandidateAction.HOLD, required_budget_w=0.0,
                    comfort_gap_c=max(0.0, temperature - room.comfort_temperature_c), confidence=room.estimate.confidence,
                    reason_code="evening_comfort_holding",
                    reason_text="V2 Abendkomfort: das laufende Gerät bleibt an, bis die vereinbarte Abendtemperatur erreicht ist (kein Auslauf ohne PV).",
                )
            if upper is not None and target is not None and target < upper:
                return RoomCandidate(
                    policy=room.policy, action=CandidateAction.ADJUST, required_budget_w=0.0,
                    comfort_gap_c=max(0.0, temperature - room.comfort_temperature_c), confidence=room.estimate.confidence,
                    reason_code="pv_wind_down", reason_text="V2 übernimmt V1-Auslauf: ohne PV wird der Gerätesollwert sofort auf die sparsame Auslaufstufe angehoben.",
                    safety_override=True, target_before_c=target, target_after_c=upper,
                )
            at_relaxed_target = upper is not None and target is not None and target >= upper
            stop_after_s = self._RELAXED_TARGET_STOP_S if at_relaxed_target else wind_down_s
            if not still_needs_evening_comfort and no_pv_for_s >= stop_after_s:
                return RoomCandidate(
                    policy=room.policy, action=CandidateAction.STOP, required_budget_w=0.0,
                    comfort_gap_c=0.0, confidence=room.estimate.confidence,
                    reason_code="pv_surplus_ended", reason_text=f"V2-Auslauf: PV-Überschuss bleibt seit {int(stop_after_s // 60)} Minuten aus; die Kühlung wird beendet.",
                    safety_override=True,
                )
            return RoomCandidate(
                policy=room.policy, action=CandidateAction.HOLD, required_budget_w=0.0,
                comfort_gap_c=max(0.0, temperature - room.comfort_temperature_c), confidence=room.estimate.confidence,
                reason_code="pv_wind_down_waiting",
                reason_text=f"V2-Auslauf: ohne PV läuft das Gerät nur auf der entspannten Stufe aus und wird nach {int(stop_after_s // 60)} Minuten abgeschaltet.",
            )
        # A learned incremental demand is required only to *start* a new
        # compressor load.  It must never veto a no-PV wind-down or stop of
        # an already running, comfortable room above.
        if room.required_budget_w is None:
            # 0.5.7: a missing learned value must not pin a room forever.  The
            # 0.5.0 refactor had silently dropped the learning feed, so this
            # branch blocked every normal start and only the acute/dead-end
            # guards could cool - the living room stayed warm while the sun was
            # still shining.  Start with a conservative split estimate; the
            # house budget still has to cover it and the learner replaces it
            # with the measured value after a few clean samples.
            budget_w = _DEFAULT_SPLIT_BUDGET_W
        else:
            budget_w = room.required_budget_w
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
        if predicted is None or room.estimate.confidence <= 0.0:
            return V2ShadowRunner._hold(room, "forecast_insufficient", "V2 wartet: Temperaturprognose oder Konfidenz reicht noch nicht für eine Modulationsstufe.")
        comfort_gap = predicted - room.comfort_temperature_c
        if comfort_gap <= 0:
            if room.observed_hvac_mode == "cool":
                current = room.observed_target_temperature_c
                upper = room.pilot_max_target_temperature_c
                if (
                    room.occupied_window_active
                    and room.estimate.temperature_c is not None
                    and room.estimate.temperature_c <= room.comfort_temperature_c - self._OCCUPIED_STOP_RESERVE_C
                ):
                    return RoomCandidate(
                        policy=room.policy,
                        action=CandidateAction.STOP,
                        required_budget_w=0.0,
                        comfort_gap_c=abs(comfort_gap),
                        confidence=room.estimate.confidence,
                        reason_code="occupied_comfort_reached",
                        reason_text="V2 Abendanwesenheit: der Raum hat den Komfort erreicht; die Kühlung wird sofort beendet (kein Zugluft-Dauerbetrieb).",
                        safety_override=True,
                    )
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
        evening_priority = evening_comfort or room.evening_deadline_at_risk
        deadline_priority = room.deadline_at_risk
        # The living room is first in the PV allocation, not a blanket right
        # to restart a stopped compressor after sunset.  A room that has just
        # completed wind-down may only restart without surplus for the explicit
        # evening-comfort promise, a sleeping-room deadline, or the hard-limit
        # failsafe handled above.
        if not usable_cooling_authority and room.observed_hvac_mode != "cool" and not evening_priority and not deadline_priority:
            return V2ShadowRunner._hold(
                room,
                "pv_start_blocked_no_surplus",
                "V2 startet keine bereits abgeschaltete Kühlung ohne nutzbare PV-Reserve; Komfort- und Fail-safe-Ausnahmen bleiben ausdrücklich möglich.",
            )
        if (
            room.occupied_window_active
            and room.observed_hvac_mode != "cool"
            and not evening_priority
            and not deadline_priority
            and comfort_gap < self._OCCUPIED_RESTART_GAP_C
        ):
            return V2ShadowRunner._hold(
                room,
                "occupied_comfort_hysteresis",
                "V2 Abendanwesenheit: kühlt erst wieder, wenn die Prognose mehr als 0,5 K über dem Komfort liegt (Zugluftschutz).",
            )
        normal_start_surplus_stable = (
            pv_available
            and now - self._pv_available_since[room.policy.room_id] >= self._NORMAL_START_SURPLUS_STABLE_S
        )
        if (
            room.observed_hvac_mode != "cool"
            and not evening_priority
            and not deadline_priority
            and pv_available
            and not normal_start_surplus_stable
        ):
            remaining_s = int(self._NORMAL_START_SURPLUS_STABLE_S - (now - self._pv_available_since[room.policy.room_id]))
            return V2ShadowRunner._hold(
                room,
                "pv_start_waiting_stable_surplus",
                f"V2 wartet noch {max(1, (remaining_s + 59) // 60)} Min. auf stabilen PV-Überschuss, bevor ein nicht priorisierter Raum neu startet.",
            )
        evening_target = None
        if evening_priority:
            # An old PV-precool target (for example 20 C) must never leak
            # into occupied evening use.  V1 immediately hands the device to
            # its evening target; V2 carries that target explicitly so the
            # planner can make the same non-aggressive transition.
            # 0.8.1: that target is the room's normal comfort, exactly like V1
            # (``evening_comfort_target = floor(comfort)``).  The evening
            # comfort value (25 C) is the *allowance* that triggers the cooling,
            # not a setpoint to overshoot: cooling towards 23 C made the room
            # colder than its own daytime comfort and burned extra energy
            # (household question 2026-09-20: "der abendkomfort heisst aber dann
            # auch dass ab diesen 25 grad was passiert").
            evening_target = max(
                room.pilot_min_target_temperature_c or room.comfort_temperature_c,
                room.comfort_temperature_c,
            )
        return RoomCandidate(
            policy=room.policy,
            action=CandidateAction.ADJUST,
            # An occupied evening promise and a hard limit may use the
            # available house capacity even when momentary export is zero.
            # They are still single, rate-limited device steps.
            required_budget_w=0.0 if evening_priority else budget_w,
            comfort_gap_c=comfort_gap,
            confidence=room.estimate.confidence,
            reason_code=(
                "evening_comfort_deadline_risk"
                if room.evening_deadline_at_risk
                else "evening_comfort_required"
                if evening_comfort
                else "sleep_deadline_risk"
                if deadline_priority
                else "forecast_comfort_risk"
            ),
            reason_text=(
                "V2 Abendkomfort-Deadline: die belastbare Prognose würde den Zielwert zum Beginn verfehlen; V2 startet deshalb eine ruhige Vorlaufstufe mit Auto-Lüfter."
                if room.evening_deadline_at_risk
                else "V2 Abendkomfort: der Raum wird trotz fehlendem PV-Export zur vereinbarten Komforttemperatur geführt."
                if evening_comfort
                else "V2 Schlafraum-Deadline: die belastbare Prognose würde den Zielwert zum Beginn verfehlen; V2 kühlt vorausschauend mit Auto-Lüfter."
                if deadline_priority
                else "V2 Shadow: Prognose zeigt eine vermeidbare Komfortüberschreitung; eine sanfte Stufe wird angefragt."
            ),
            safety_override=evening_priority or deadline_priority,
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
