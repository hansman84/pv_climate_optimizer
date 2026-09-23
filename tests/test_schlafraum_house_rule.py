"""Hausregel Schlafraeume und die Raum-Identitaet des Schlafzimmers (0.16.0).

Zwei Dinge werden hier geprueft:

1. HAUSREGEL: Die Schlafraeume (Zimmer mit der Kennung climate.schlafzimmer bzw.
   climate.kinderzimmer) werden nur ab 25,0 C Aussentemperatur vorgekuehlt, ihr
   Vorkuehl-/Kuehlziel ist 23,0 C (nicht 22,0).  Das sind Defaults der
   Integration; ausdruecklich gesetzte Nutzerwerte bleiben unangetastet.
2. BUGFIX: Ein Komfortziel, das ueber die Raumkennung gesetzt wird, wird danach
   auch gelesen - auch wenn der Raum historisch anders geschrieben war
   ("Schlafzimmrt").  Vorher fand der Lookup den Raum nicht mehr: Lesen None,
   Schreiben wirkungslos.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PACKAGE = "pv_climate_controller"
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / PACKAGE


def _load(module: str):
    """Modul ohne Home-Assistant-Installation laden (wie die anderen Tests)."""
    sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE)).__path__ = [str(ROOT)]
    path = ROOT / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{module}", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


const = _load("const")
models = _load("models")
controller = _load("controller")


def _zone(**overrides) -> dict:
    """Eine gespeicherte Zone, wie sie in den Optionen steht."""
    base = {
        "zone_id": "climate.schlafzimmer",
        "name": "Schlafzimmer",
        "climate_entity_id": "climate.schlafzimmer",
        "temperature_entity_id": "sensor.schlafzimmer_temp",
    }
    base.update(overrides)
    return base


def _load_zones(*zones: dict):
    return controller.PVClimateController.from_config({"shadow_mode": True}, {"house_zones": list(zones)})


# ---------------------------------------------------------------------------
# 1. Hausregel Schlafraeume
# ---------------------------------------------------------------------------


def test_schlafraum_defaults_are_applied_to_sleeping_rooms() -> None:
    """Vorkuehlen ab 25,0 C und Ziel 23,0 C gelten als Integrations-Default."""
    runtime = _load_zones(
        _zone(),
        _zone(zone_id="climate.kinderzimmer", name="Kinderzimmer", climate_entity_id="climate.kinderzimmer"),
        _zone(zone_id="climate.spielzimmer", name="Spielzimmer", climate_entity_id="climate.spielzimmer"),
        _zone(zone_id="climate.wohnzimmer", name="Wohnzimmer", climate_entity_id="climate.wohnzimmer"),
    )

    bedroom = runtime.zone_by_room_id("climate.schlafzimmer")
    child_room = runtime.zone_by_room_id("climate.kinderzimmer")
    play_room = runtime.zone_by_room_id("climate.spielzimmer")
    living_room = runtime.zone_by_room_id("climate.wohnzimmer")

    assert bedroom is not None and child_room is not None
    assert bedroom.comfort_temperature == models.SCHLAFRAUM_ZIEL_C
    assert bedroom.min_outdoor_cooling_temperature_c == models.SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C
    assert child_room.comfort_temperature == models.SCHLAFRAUM_ZIEL_C
    assert child_room.min_outdoor_cooling_temperature_c == models.SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C
    # Das Geraetesoll wird in den Schlafraeumen nicht mehr auf 22,0 C gezogen.
    assert bedroom.pilot_min_target_temperature == models.SCHLAFRAUM_ZIEL_C
    # Uebriges Obergeschoss und Erdgeschoss behalten ihre bisherigen Defaults.
    assert play_room.min_outdoor_cooling_temperature_c == 20.0
    assert living_room.min_outdoor_cooling_temperature_c is None
    assert living_room.comfort_temperature == 23.5


def test_explicit_user_values_are_never_overwritten_by_the_house_rule() -> None:
    """Nur ungesetzte Felder bekommen die Hausregel - gesetzte bleiben."""
    runtime = _load_zones(_zone(
        comfort_temperature=22.0,
        min_outdoor_cooling_temperature_c=0.0,  # Regel bewusst ausgeschaltet
        pilot_min_target_temperature=20.0,
    ))

    bedroom = runtime.zone_by_room_id("climate.schlafzimmer")

    assert bedroom is not None
    assert bedroom.comfort_temperature == 22.0
    assert bedroom.min_outdoor_cooling_temperature_c == 0.0
    assert bedroom.pilot_min_target_temperature == 20.0


def test_house_rule_is_applied_to_the_other_house_zones_too() -> None:
    """Kein Schlafraum bleibt ohne Regel: die Nachbarzonen bleiben unberuehrt."""
    runtime = _load_zones(
        _zone(),
        _zone(zone_id="climate.speis", name="Speis", climate_entity_id="climate.speis"),
    )

    assert runtime.zone_by_room_id("climate.speis").comfort_temperature == 23.5
    assert runtime.zone_by_room_id("climate.speis").min_outdoor_cooling_temperature_c is None


def test_schlafraum_is_recognized_by_room_id_label_and_old_spelling() -> None:
    assert models.is_schlafraum(room_id="climate.schlafzimmer")
    assert models.is_schlafraum(room_id="climate.kinderzimmer")
    assert models.is_schlafraum(name="Schlafzimmer")
    assert models.is_schlafraum(name="Schlafzimmrt")  # alte Schreibweise
    assert not models.is_schlafraum(name="Spielzimmer")
    assert not models.is_schlafraum(room_id="climate.wohnzimmer")
    assert not models.is_schlafraum(name="Speis")


def test_schlafraum_defaults_are_the_documented_house_values() -> None:
    assert models.SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C == 25.0
    assert models.SCHLAFRAUM_ZIEL_C == 23.0
    assert models.SCHLAFRAUM_ROOM_IDS == ("climate.schlafzimmer", "climate.kinderzimmer")
    assert models.zone_min_outdoor_cooling_default(room_id="climate.schlafzimmer") == 25.0
    assert models.zone_min_outdoor_cooling_default(name="Kinderzimmer") == 25.0
    assert models.zone_min_outdoor_cooling_default(name="Spielzimmer") == 20.0
    assert models.zone_min_outdoor_cooling_default(name="Speis") is None


# ---------------------------------------------------------------------------
# 2. Bugfix: Komfortziel des Schlafzimmers setzen und danach lesen
# ---------------------------------------------------------------------------


def test_comfort_target_set_for_the_sleeping_room_is_read_back() -> None:
    """BUGFIX 0.16.0: setzen ueber die Raumkennung, danach lesen - 24,0 C."""
    runtime = _load_zones(
        _zone(zone_id="configured_zone", name="Wohnzimmer", climate_entity_id="climate.wohnzimmer"),
        _zone(),
    )

    runtime.set_zone_thermal_settings("climate.schlafzimmer", comfort_temperature=24.0)

    bedroom = runtime.zone_by_room_id("climate.schlafzimmer")
    assert bedroom is not None
    assert bedroom.comfort_temperature == 24.0
    # Die Nachbarzone bleibt unangetastet.
    assert runtime.zone_by_room_id("configured_zone").comfort_temperature == 23.5


def test_comfort_target_survives_the_options_reload() -> None:
    """Nach dem Serialisieren (HA laedt die Optionen neu) gilt der Wert weiter."""
    runtime = _load_zones(_zone())
    runtime.set_zone_thermal_settings("climate.schlafzimmer", comfort_temperature=24.0)

    persisted = [controller.serialize_zone_config(zone) for zone in runtime.config.house_zones]
    reloaded = _load_zones(*persisted)

    assert reloaded.zone_by_room_id("climate.schlafzimmer").comfort_temperature == 24.0


def test_comfort_target_reaches_a_room_that_keeps_its_old_spelling() -> None:
    """Alte Kennung/Schreibweise darf das Schreiben nicht mehr verschlucken."""
    runtime = _load_zones(_zone(
        zone_id="Schlafzimmrt",
        name="Schlafzimmrt",
        climate_entity_id="climate.schlafzimmer",
    ))

    runtime.set_zone_thermal_settings("climate.schlafzimmer", comfort_temperature=24.0)

    assert runtime.zone_by_room_id("Schlafzimmrt").comfort_temperature == 24.0
    assert runtime.zone_by_room_id("Schlafzimmer").comfort_temperature == 24.0
    assert runtime.zone_by_room_id("climate.schlafzimmer").comfort_temperature == 24.0


def test_legacy_room_spelling_is_migrated_to_the_canonical_label() -> None:
    runtime = _load_zones(_zone(
        zone_id="Schlafzimmrt",
        name="Schlafzimmrt",
        climate_entity_id="climate.schlafzimmer",
    ))

    bedroom = runtime.zone_by_room_id("climate.schlafzimmer")

    assert bedroom is not None
    assert bedroom.name == "Schlafzimmer"
    # Die Hausregel greift auch fuer die alte Schreibweise.
    assert bedroom.comfort_temperature == models.SCHLAFRAUM_ZIEL_C
    assert bedroom.min_outdoor_cooling_temperature_c == models.SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C


def test_unknown_room_still_writes_nothing() -> None:
    """Ein wirklich unbekannter Raum bleibt folgenlos (kein Geistereintrag)."""
    runtime = _load_zones(_zone())
    before = runtime.config.house_zones

    runtime.set_zone_thermal_settings("climate.gaestezimmer", comfort_temperature=24.0)

    assert runtime.config.house_zones == before
    assert runtime.zone_by_room_id("climate.gaestezimmer") is None


def test_legacy_entity_ids_are_renamed_to_the_canonical_room_name() -> None:
    """Die HA-Entitaets-IDs des Schlafzimmers heissen kuenftig richtig.

    Genau diese IDs tragen in Produktion noch die alte Schreibweise; dadurch war
    number.pv_klimaregler_schlafzimmer_komforttemperatur nicht adressierbar.
    """
    assert models.legacy_entity_id_rename(
        "number.pv_klimaregler_schlafzimmrt_komforttemperatur"
    ) == "number.pv_klimaregler_schlafzimmer_komforttemperatur"
    assert models.legacy_entity_id_rename(
        "number.pv_klimaregler_schlafzimmrt_harte_temperaturgrenze"
    ) == "number.pv_klimaregler_schlafzimmer_harte_temperaturgrenze"
    assert models.legacy_entity_id_rename(
        "sensor.pv_klimaregler_schlafzimmrt_temperaturprognose"
    ) == "sensor.pv_klimaregler_schlafzimmer_temperaturprognose"
    # Korrekte IDs und fremde Raeume bleiben unangetastet.
    assert models.legacy_entity_id_rename("number.pv_klimaregler_schlafzimmer_komforttemperatur") is None
    assert models.legacy_entity_id_rename("number.pv_klimaregler_kinderzimmer_komforttemperatur") is None
    assert models.legacy_entity_id_rename("climate.schlafzimmer") is None
    assert models.legacy_entity_id_rename("") is None
