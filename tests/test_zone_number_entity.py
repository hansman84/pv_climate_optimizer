"""Entitaets-Tests der Raum-Regler ohne Home-Assistant-Installation.

Die Integration laeuft in Home Assistant; im Test-venv ist HA nicht installiert.
Deshalb werden hier nur die HA-Bausteine gestubbt, die ``number.py`` beim Import
braucht (Entity-Basis, NumberEntity-Basis, Konstanten).  Geprueft wird die
echte Lese-/Schreiblogik der Entitaeten gegen einen echten
``PVClimateController`` samt Persistenz-Roundtrip ueber die Optionen.

Hintergrund (0.16.0): ``number.pv_klimaregler_schlafzimmer_komforttemperatur``
las sich als ``None`` und ein ``number.set_value`` blieb wirkungslos, waehrend
alle anderen Raeume funktionierten.  Ursache war die Raumaufloesung: die
Entitaet verglich rohe Zeichenketten, der Schlafzimmer-Raum war historisch aber
"Schlafzimmrt" geschrieben.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

PACKAGE = "pv_climate_controller"
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / PACKAGE


# ---------------------------------------------------------------------------
# HA-Stubs
# ---------------------------------------------------------------------------


class _StubEntity:
    """Minimale Entity-Basis (nur was die Integration benutzt)."""

    _attr_has_entity_name = False


class _StubNumberEntity:
    """Minimale NumberEntity-Basis."""


class _AutoAttributes(type):
    """Liefert unbekannte Klassen-Attribute als kleingeschriebene Zeichenkette."""

    def __getattr__(cls, name: str) -> str:
        return name.lower()


class _StubNumberDeviceClass(metaclass=_AutoAttributes):
    TEMPERATURE = "temperature"


class _StubEntityCategory(metaclass=_AutoAttributes):
    CONFIG = "config"


class _StubUnitOfTemperature(metaclass=_AutoAttributes):
    CELSIUS = "°C"


class _StubUnitOfPower(metaclass=_AutoAttributes):
    WATT = "W"


def _stub_module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_homeassistant_stubs() -> None:
    try:  # pragma: no cover - im Test-venv nicht installiert
        import homeassistant  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - echte Installation hat Vorrang
        return
    _stub_module("homeassistant")
    _stub_module("homeassistant.components")
    _stub_module("homeassistant.helpers")
    _stub_module(
        "homeassistant.components.number",
        NumberDeviceClass=_StubNumberDeviceClass,
        NumberEntity=_StubNumberEntity,
    )
    _stub_module(
        "homeassistant.const",
        EntityCategory=_StubEntityCategory,
        UnitOfPower=_StubUnitOfPower,
        UnitOfTemperature=_StubUnitOfTemperature,
    )
    _stub_module("homeassistant.core", HomeAssistant=object)
    _stub_module("homeassistant.config_entries", ConfigEntry=object)
    _stub_module("homeassistant.helpers.entity", Entity=_StubEntity)
    _stub_module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)


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


_install_homeassistant_stubs()
const = _load("const")
models = _load("models")
controller = _load("controller")
number = _load("number")


# ---------------------------------------------------------------------------
# Kleiner HA-Ersatz: die Entitaet persistiert ihre Zone ueber die Optionen
# ---------------------------------------------------------------------------


class _FakeConfigEntry:
    def __init__(self, entry_id: str, options: dict) -> None:
        self.entry_id = entry_id
        self.options = dict(options)


class _FakeConfigEntries:
    def __init__(self, entry: _FakeConfigEntry) -> None:
        self._entry = entry

    def async_get_entry(self, entry_id: str):
        return self._entry if entry_id == self._entry.entry_id else None

    def async_update_entry(self, entry: _FakeConfigEntry, **kwargs: object) -> None:
        if "options" in kwargs:
            entry.options = dict(kwargs["options"])  # type: ignore[arg-type]


class _FakeHass:
    def __init__(self, entry: _FakeConfigEntry) -> None:
        self.config_entries = _FakeConfigEntries(entry)


ENTRY_ID = "test-entry"


def _persisted_zone(**overrides) -> dict:
    base = {
        "zone_id": "climate.schlafzimmer",
        "name": "Schlafzimmer",
        "climate_entity_id": "climate.schlafzimmer",
        "temperature_entity_id": "sensor.schlafzimmer_temp",
    }
    base.update(overrides)
    return base


def _runtime_and_hass(zone: dict):
    options = {"house_zones": [zone]}
    runtime = controller.PVClimateController.from_config({"shadow_mode": True}, options)
    entry = _FakeConfigEntry(ENTRY_ID, options)
    return runtime, _FakeHass(entry), entry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_comfort_number_reads_the_house_rule_default() -> None:
    runtime, hass, _ = _runtime_and_hass(_persisted_zone())
    entity = number.ZoneComfortTemperatureNumber(runtime, ENTRY_ID, "zone_comfort_temperature_4", "climate.schlafzimmer")
    entity.hass = hass

    assert entity.native_value == models.SCHLAFRAUM_ZIEL_C


def test_comfort_number_reads_the_value_it_wrote_for_the_sleeping_room() -> None:
    """BUGFIX: setzen (24,0) und danach lesen - der Schreibvorgang kommt an.

    0.16.1: Der Schreibvorgang landet weiterhin am Raum (Lesen 24,0).  Nach dem
    Neuladen der Optionen setzt die Hausregel fuer dieses Zimmer aber wieder
    23,0 C durch - der Handwert ist nur noch im laufenden Betrieb sichtbar.
    """
    runtime, hass, entry = _runtime_and_hass(_persisted_zone())
    entity = number.ZoneComfortTemperatureNumber(runtime, ENTRY_ID, "zone_comfort_temperature_4", "climate.schlafzimmer")
    entity.hass = hass

    asyncio.run(entity.async_set_native_value(24.0))

    assert entity.native_value == 24.0
    # Home Assistant schreibt die Optionen und laedt die Integration neu: dort
    # gilt fuer dieses Zimmer die Hausregel (23,0 C).
    reloaded = controller.PVClimateController.from_config(
        {"shadow_mode": True}, {"house_zones": entry.options["house_zones"]}
    )
    fresh = number.ZoneComfortTemperatureNumber(reloaded, ENTRY_ID, "zone_comfort_temperature_4", "climate.schlafzimmer")
    fresh.hass = hass

    assert fresh.native_value == models.SCHLAFRAUM_ZIEL_C == 23.0


def test_comfort_number_of_the_legacy_room_key_no_longer_reads_none() -> None:
    """Alte Raumschreibweise: vorher None und wirkungslos, jetzt normal."""
    runtime, hass, _ = _runtime_and_hass(_persisted_zone(zone_id="Schlafzimmrt", name="Schlafzimmrt"))
    entity = number.ZoneComfortTemperatureNumber(runtime, ENTRY_ID, "zone_comfort_temperature_4", "Schlafzimmrt")
    entity.hass = hass

    assert entity.native_value == models.SCHLAFRAUM_ZIEL_C
    assert entity.name == "Schlafzimmer – Komforttemperatur"

    asyncio.run(entity.async_set_native_value(24.0))

    assert entity.native_value == 24.0
    assert runtime.zone_by_room_id("climate.schlafzimmer").comfort_temperature == 24.0


def test_comfort_number_writes_through_the_canonical_room_id() -> None:
    """Die kanonische Kennung erreicht einen Raum mit alter Schreibweise."""
    runtime, hass, _ = _runtime_and_hass(_persisted_zone(zone_id="Schlafzimmrt", name="Schlafzimmrt"))
    entity = number.ZoneComfortTemperatureNumber(runtime, ENTRY_ID, "zone_comfort_temperature_4", "climate.schlafzimmer")
    entity.hass = hass

    asyncio.run(entity.async_set_native_value(24.0))

    assert entity.native_value == 24.0


def test_comfort_number_of_a_removed_room_reads_none_and_ignores_writes() -> None:
    """Nur ein wirklich unbekannter Raum bleibt None (kein Geisterwert)."""
    runtime, hass, _ = _runtime_and_hass(_persisted_zone())
    entity = number.ZoneComfortTemperatureNumber(runtime, ENTRY_ID, "zone_comfort_temperature_9", "climate.gaestezimmer")
    entity.hass = hass

    assert entity.native_value is None

    asyncio.run(entity.async_set_native_value(24.0))

    assert entity.native_value is None


def test_min_outdoor_number_shows_the_house_rule_threshold() -> None:
    runtime, hass, _ = _runtime_and_hass(_persisted_zone())
    entity = number.ZoneMinOutdoorCoolingNumber(runtime, ENTRY_ID, "zone_min_outdoor_cooling_4", "climate.schlafzimmer")
    entity.hass = hass

    assert entity.native_value == models.SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C == 25.0


def test_min_outdoor_number_enforces_the_house_rule() -> None:
    """0.16.1: Die Hausregel hat Vorrang - auch vor einem alten "0 = aus"."""
    runtime, hass, _ = _runtime_and_hass(_persisted_zone(min_outdoor_cooling_temperature_c=0.0))
    entity = number.ZoneMinOutdoorCoolingNumber(runtime, ENTRY_ID, "zone_min_outdoor_cooling_4", "climate.schlafzimmer")
    entity.hass = hass

    assert entity.native_value == models.SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C == 25.0


def test_pilot_min_number_no_longer_pulls_the_room_to_22() -> None:
    runtime, hass, _ = _runtime_and_hass(_persisted_zone())
    entity = number.ZonePilotMinTargetTemperatureNumber(runtime, ENTRY_ID, "zone_pilot_min_target_temperature_4", "climate.schlafzimmer")
    entity.hass = hass

    assert entity.native_value == models.SCHLAFRAUM_ZIEL_C
