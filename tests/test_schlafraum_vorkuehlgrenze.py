"""Hausregel-Aussengrenze im Schlafraum-Pfad (0.16.1).

Live-Befund (23.09.2026): Das Schlafzimmer kuehlte bei 18,4 C Aussentemperatur
weiter (Geraet state=cool, Soll 23), obwohl die Hausregel "Vorkuehlen nur ab
25,0 C aussen" gilt.

Zwei Ursachen, beide hier belegt:

1. Der Bedroom-Precool-Zweig in ``v2_shadow.py`` (reason_code
   ``bedroom_schedule_pending`` bzw. ``bedroom_quiet_time``) pruefte nur die
   Bedienungslage (Uhrzeit), nicht die Aussengrenze.  Die Aussengrenze ist
   jetzt Pflichtbedingung; unter der Grenze wird gehalten bzw. ein laufendes
   Geraet beendet.
2. ``settle_plan`` in ``v2_command_planner.py`` zog ein laufendes Geraet ohne
   Aussengrenze auf dem Komfortziel nach, und der Sollwert-Daempfer konnte
   eine regulaere Stopp-Anforderung verschlucken - dadurch lief das Geraet
   weiter.  Ein Stopp wird jetzt nie gedaempft, und ``settle_plan`` beendet
   einen Raum unterhalb seiner Aussengrenze.

Geprueft wird mit reinen V2-Bausteinen (keine Home-Assistant-Zugriffe).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PACKAGE = "pv_climate_controller_vorkuehlgrenze_test"
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "pv_climate_controller"


def _load(module: str):
    package = sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE))
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{module}", ROOT / f"{module}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


models = _load("v2_models")
shadow = _load("v2_shadow")
command_planner = _load("v2_command_planner")

SCHLAFRAUM_AUSSENGRENZE_C = 25.0  # models.SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C
KOMPORT_C = 23.0                  # models.SCHLAFRAUM_ZIEL_C


def _outdoor(value: float | None, quality: str = "valid"):
    if value is None:
        return models.InputValue(
            "sensor.aussentemperatur", None, "°C", None, models.InputQuality.MISSING, "not_configured"
        )
    return models.InputValue(
        "sensor.aussentemperatur",
        value,
        "°C",
        5.0,
        models.InputQuality.VALID if quality == "valid" else models.InputQuality.STALE,
        "source_fresh" if quality == "valid" else "source_stale",
    )


def _bedroom_room(
    *,
    outdoor_c: float | None,
    air_c: float = 24.0,
    comfort_c: float = KOMPORT_C,
    eligibility: object | None = None,
    hvac_mode: str = "off",
    target_c: float | None = None,
    pv_export_w: float = 500.0,
    predicted_c: float = 25.0,
    budget_w: float | None = 400.0,
) -> object:
    """Ein Schlafzimmer-Eingang mit echten Aussengrenze = 25,0 C."""
    valid_temperature = models.InputValue(
        "sensor.schlafzimmer", air_c, "°C", 10.0, models.InputQuality.VALID, "fresh"
    )
    valid_flag = models.InputValue("sensor.flag", True, None, 1.0, models.InputQuality.VALID, "allowed")
    vacation_flag = models.InputValue(
        "input_boolean.vacation", False, None, 1.0, models.InputQuality.VALID, "not_active"
    )
    usable_export = models.InputValue(
        "sensor.export", pv_export_w, "W", 1.0, models.InputQuality.VALID, "usable_surplus"
    )
    snapshot = models.InputSnapshot(
        "2026-09-23T14:00:00+00:00",
        valid_temperature,
        valid_flag,
        usable_export,
        valid_flag,
        _outdoor(outdoor_c),
        valid_flag,
        valid_flag,
        vacation_flag,
        valid_flag,
    )
    return models.V2RoomInput(
        models.RoomPolicy("climate.schlafzimmer", "Schlafzimmer", 20),
        snapshot,
        models.RoomEstimate(
            "climate.schlafzimmer", air_c, 0.2, predicted_c, 0.8, -0.7, ("trend",), "forecast_ready"
        ),
        eligibility
        or models.EligibilityDecision(
            False, "bedroom_schedule_pending", "Schlafzimmer: Vorkühlung beginnt ab 15:30 Uhr."
        ),
        comfort_c,
        25.5,
        budget_w,
        observed_hvac_mode=hvac_mode,
        observed_target_temperature_c=target_c,
        pilot_min_target_temperature_c=23.0,
        pilot_max_target_temperature_c=24.0,
        target_temperature_step_c=1.0,
        min_outdoor_cooling_temperature_c=SCHLAFRAUM_AUSSENGRENZE_C,
    )


# ---------------------------------------------------------------------------
# 1. Bedroom-Precool-Zweig prueft die Aussengrenze (Pflichtbedingung)
# ---------------------------------------------------------------------------


def test_bedroom_precool_holds_at_18_4_c_outdoor() -> None:
    """18,4 C aussen, Raum 24,0 C, Komfort 23,0 C: halten, kein Start/Kuehlen."""
    room = _bedroom_room(outdoor_c=18.4, air_c=24.0, comfort_c=KOMPORT_C, hvac_mode="off", target_c=23.0)

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    assert candidates[0].action is models.CandidateAction.HOLD
    assert candidates[0].reason_code == "outdoor_too_cold_no_cooling"
    assert not candidates[0].requests_modulation
    assert decision.approved_room_ids == ()


def test_bedroom_precool_stops_a_running_unit_at_18_4_c_outdoor() -> None:
    """Laufendes Geraet bei 18,4 C aussen: V2 beendet die Kuehlung."""
    room = _bedroom_room(outdoor_c=18.4, air_c=24.0, comfort_c=KOMPORT_C, hvac_mode="cool", target_c=23.0)

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].action is models.CandidateAction.STOP
    assert candidates[0].reason_code == "outdoor_too_cold_no_cooling"
    assert candidates[0].safety_override
    assert plan is not None and plan.action is models.CandidateAction.STOP


def test_bedroom_quiet_time_branch_also_checks_the_outdoor_floor() -> None:
    """Auch im Ruhezeit-Zweig gilt die Aussengrenze (dieselbe Pflichtbedingung)."""
    room = _bedroom_room(
        outdoor_c=18.4,
        air_c=24.0,
        hvac_mode="off",
        target_c=23.0,
        eligibility=models.EligibilityDecision(
            False, "bedroom_quiet_time", "Schlafzimmer: Vorkühlung endet um 18:30 Uhr."
        ),
    )

    candidates, _ = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    assert candidates[0].action is models.CandidateAction.HOLD
    assert candidates[0].reason_code == "outdoor_too_cold_no_cooling"


def test_bedroom_precool_is_allowed_at_26_c_outdoor() -> None:
    """26,0 C aussen, Raum 24,0 C, Komfort 23,0 C: Kuehlen ist erlaubt."""
    room = _bedroom_room(
        outdoor_c=26.0,
        air_c=24.0,
        comfort_c=KOMPORT_C,
        hvac_mode="cool",
        target_c=24.0,
        eligibility=models.EligibilityDecision(True, "v2_eligible", "V2 bewertet die freigegebenen Quellen."),
    )

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)
    plan = command_planner.V2CommandPlanner().plan(room, candidates[0], decision)

    assert candidates[0].action is models.CandidateAction.ADJUST
    assert candidates[0].reason_code == "forecast_comfort_risk"
    assert candidates[0].reason_code != "outdoor_too_cold_no_cooling"
    assert plan is not None and plan.action is models.CandidateAction.ADJUST


def test_missing_outdoor_source_does_not_block_but_keeps_the_schedule_rule() -> None:
    """Ohne gueltige Aussenquelle blockiert die Grenze nicht (fail-open)."""
    room = _bedroom_room(outdoor_c=None, air_c=24.0, hvac_mode="off", target_c=23.0)

    candidates, _ = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    assert candidates[0].action is models.CandidateAction.HOLD
    assert candidates[0].reason_code == "bedroom_schedule_pending"


def test_stale_outdoor_source_does_not_block() -> None:
    """Auch eine veraltete Aussenquelle blockiert nicht (weiter fail-open)."""
    room = _bedroom_room(outdoor_c=18.4, air_c=24.0, hvac_mode="off", target_c=23.0)
    stale = models.V2RoomInput(
        room.policy,
        models.InputSnapshot(
            room.snapshot.observed_at,
            room.snapshot.room_temperature,
            room.snapshot.climate_available,
            room.snapshot.pv_export_w,
            room.snapshot.outdoor_unit_power_w,
            _outdoor(18.4, quality="stale"),
            room.snapshot.heat_pump_priority,
            room.snapshot.automation_enabled,
            room.snapshot.vacation_active,
            room.snapshot.cooling_season_allowed,
        ),
        room.estimate,
        room.eligibility,
        room.comfort_temperature_c,
        room.hard_max_temperature_c,
        room.required_budget_w,
        observed_hvac_mode=room.observed_hvac_mode,
        observed_target_temperature_c=room.observed_target_temperature_c,
        pilot_min_target_temperature_c=room.pilot_min_target_temperature_c,
        pilot_max_target_temperature_c=room.pilot_max_target_temperature_c,
        target_temperature_step_c=room.target_temperature_step_c,
        min_outdoor_cooling_temperature_c=SCHLAFRAUM_AUSSENGRENZE_C,
    )

    candidates, _ = shadow.V2ShadowRunner().evaluate((stale,), available_budget_w=500.0)

    assert candidates[0].reason_code == "bedroom_schedule_pending"


def test_hard_temperature_limit_stays_the_emergency_net() -> None:
    """Die harte Temperaturgrenze bleibt das Sicherheitsnetz unter der Grenze."""
    room = _bedroom_room(outdoor_c=18.4, air_c=26.0, comfort_c=KOMPORT_C, hvac_mode="off", target_c=23.0)

    candidates, decision = shadow.V2ShadowRunner().evaluate((room,), available_budget_w=500.0)

    assert candidates[0].action is models.CandidateAction.START
    assert candidates[0].reason_code == "hard_temperature_limit_failsafe"
    assert decision.approved_room_ids == ("climate.schlafzimmer",)


# ---------------------------------------------------------------------------
# 2. Ausfuehrungspfad: kein Nachziehen und kein gedaempfter Stopp
# ---------------------------------------------------------------------------


def test_settle_plan_does_not_settle_a_room_below_its_outdoor_floor() -> None:
    """settle_plan darf ein laufendes Geraet unter der Grenze nicht weiterziehen."""
    room = _bedroom_room(outdoor_c=18.4, air_c=24.0, comfort_c=KOMPORT_C, hvac_mode="cool", target_c=23.0)

    plan = command_planner.V2CommandPlanner().settle_plan(room)

    assert plan is not None
    assert plan.action is models.CandidateAction.STOP
    assert plan.reason_code == "outdoor_too_cold_no_cooling"


def test_settle_plan_still_settles_a_room_above_its_outdoor_floor() -> None:
    """Oberhalb der Grenze bleibt das ruhige Nachziehen erhalten."""
    room = _bedroom_room(outdoor_c=26.0, air_c=24.0, comfort_c=KOMPORT_C, hvac_mode="cool", target_c=23.0)

    plan = command_planner.V2CommandPlanner().settle_plan(room)

    assert plan is None or plan.action is not models.CandidateAction.STOP


def test_rule_stop_is_never_damped_by_the_setpoint_damper() -> None:
    """Ein Stopp wird sofort geplant, auch direkt nach einem Sollwertwechsel."""
    now = [1000.0]
    planner = command_planner.V2CommandPlanner(now_fn=lambda: now[0])
    room = _bedroom_room(
        outdoor_c=26.0,
        air_c=25.0,
        comfort_c=KOMPORT_C,
        hvac_mode="cool",
        target_c=25.0,
        eligibility=models.EligibilityDecision(True, "v2_eligible", "V2 bewertet die freigegebenen Quellen."),
    )
    cool_step = models.RoomCandidate(
        room.policy,
        models.CandidateAction.ADJUST,
        0.0,
        2.0,
        0.8,
        "forecast_comfort_risk",
        "Prognose begruendet eine sanfte Modulationsstufe.",
        target_after_c=23.0,
    )
    rule_stop = models.RoomCandidate(
        room.policy,
        models.CandidateAction.STOP,
        0.0,
        0.0,
        0.8,
        "bedroom_schedule_pending",
        "Schlafzimmer: Vorkühlung beginnt ab 15:30 Uhr.",
        safety_override=True,
    )
    decision = models.HouseDecision(
        room_decisions=(),
        approved_room_ids=("climate.schlafzimmer",),
        reserved_budget_w=0.0,
        available_budget_w=500.0,
    )

    assert planner.plan(room, cool_step, decision) is not None
    now[0] += 60.0  # eine Minute spaeter: der Daempfer wuerde noch greifen
    stop_plan = planner.plan(room, rule_stop, decision)

    assert stop_plan is not None
    assert stop_plan.action is models.CandidateAction.STOP
