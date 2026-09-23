"""Pure data models for controller decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .const import EnergyPolicy, ZoneState

# ---------------------------------------------------------------------------
# Raumkennungen und alte Schreibweisen
# ---------------------------------------------------------------------------
# Eine fruehere Konfiguration schrieb den Raum "Schlafzimmrt".  Bisher wurde
# nur das Anzeige-Label korrigiert - die Raumkennung selbst blieb die alte
# Schreibweise.  Folge in Home Assistant: die Entitaeten des Raums hiessen
# weiter ..._schlafzimmrt_..., waren also unter ihrem richtigen Namen nicht
# adressierbar (Lesen ergab None, number.set_value verpuffte lautlos), waehrend
# alle anderen Raeume normal funktionierten.
ZONE_LABEL_ALIASES: dict[str, str] = {"Schlafzimmrt": "Schlafzimmer"}

# ---------------------------------------------------------------------------
# Hausregel Schlafraeume (Hauswunsch 23.09.2026)
#
# Die Schlafraeume (Schlafzimmer, Kinderzimmer) werden NUR vorgekuehlt, wenn die
# Aussentemperatur mindestens 25,0 C betraegt, und ihr Vorkuehl-/Kuehlziel ist
# 23,0 C (nicht 22,0).  Seit 0.16.1 hat die Hausregel fuer diese beiden Raeume
# Vorrang vor einem alten Handwert: Komfort 23,0 C, akute Kuehlgrenze 25,0 C und
# "Kuehlung erst ab Aussentemperatur" 25,0 C werden beim Laden der Optionen
# durchgesetzt (models.schlafraum_house_rule_values, controller._house_zones).
# Fuer alle uebrigen Raeume bleiben explizit gesetzte Werte unangetastet.
# ---------------------------------------------------------------------------
SCHLAFRAUM_ROOM_IDS: tuple[str, ...] = ("climate.schlafzimmer", "climate.kinderzimmer")
SCHLAFRAUM_ZIEL_C = 23.0
SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C = 25.0
# Akute Kuehlgrenze der Schlafraeume (Hausregel 0.16.1): ab 25,0 C Raumluft
# wird gekuehlt, auch ohne PV-Reserve - das ist dieselbe Linie wie die
# Aussengrenze, nicht mehr die alte Handgrenze 24,0 C.
SCHLAFRAUM_AKUTE_KUEHLGRENZE_C = 25.0
SCHLAFRAUM_LABELS: frozenset[str] = frozenset({"schlafzimmer", "kinderzimmer"})
# Raeume im Obergeschoss; dort gilt (ausser in den Schlafraeumen) der weiche
# Aussenboden 20,0 C aus 0.4.58.
UPSTAIRS_ZONE_LABELS: frozenset[str] = frozenset({"schlafzimmer", "kinderzimmer", "spielzimmer"})
DEFAULT_ZONE_COMFORT_C = 23.5
DEFAULT_UPSTAIRS_MIN_OUTDOOR_C = 20.0


def _label_key(name: object) -> str:
    """Vergleichsschluessel eines Raumnamens (Leerraum, Gross-/Kleinschrift)."""
    return str(name or "").strip().casefold()


def canonical_zone_label(name: object) -> str:
    """Korrigierte Schreibweise eines Raumnamens (bekannte Tippfehler)."""
    normalized = " ".join(str(name or "").split())
    return ZONE_LABEL_ALIASES.get(normalized, normalized)


def _legacy_label_keys(canonical_label: object) -> tuple[str, ...]:
    """Alle alten Schreibweisen, die denselben Raum bezeichnen."""
    key = _label_key(canonical_zone_label(canonical_label))
    return tuple(
        _label_key(legacy)
        for legacy, canonical in ZONE_LABEL_ALIASES.items()
        if _label_key(canonical) == key
    )


def canonical_room_id(zone_id: object, *, name: object = "", climate_entity_id: object = "") -> str:
    """Stabile Raumkennung einer Zone.

    Vorzug hat die Klima-Entity-ID des Raums (z. B. ``climate.schlafzimmer``).
    Alte Konfigurationen speicherten unter ``zone_id`` nur den Raumnamen (teils
    mit Tippfehler); dann wird die Kennung daraus abgeleitet, damit Lesen und
    Schreiben wirklich denselben Raum treffen.
    """
    raw = str(zone_id or "").strip()
    if "." in raw:
        return raw
    climate = str(climate_entity_id or "").strip()
    if climate:
        return climate
    return _label_key(canonical_zone_label(raw or name)) or raw


def room_lookup_keys(
    zone_id: object,
    *,
    name: object = "",
    climate_entity_id: object = "",
) -> tuple[str, ...]:
    """Alle Kennungen, unter denen dieser Raum adressiert werden darf.

    Enthaelt die kanonische Kennung, die gespeicherte Kennung, die
    Klima-Entity-ID, den Raumnamen und die alte (Tippfehler-)Schreibweise.
    """
    keys: list[str] = []
    for candidate in (
        canonical_room_id(zone_id, name=name, climate_entity_id=climate_entity_id),
        zone_id,
        climate_entity_id,
        canonical_zone_label(name),
        canonical_zone_label(zone_id),
        *_legacy_label_keys(name),
        *_legacy_label_keys(zone_id),
    ):
        key = _label_key(candidate)
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def zone_matches_room(zone: "ZoneConfig", room_id: object) -> bool:
    """True, wenn ``room_id`` diesen Raum adressiert (auch alte Schreibweise)."""
    wanted = _label_key(room_id)
    if not wanted:
        return False
    return wanted in room_lookup_keys(
        zone.zone_id, name=zone.name, climate_entity_id=zone.climate_entity_id
    )


def find_zone(zones: Iterable["ZoneConfig"], room_id: object) -> "ZoneConfig | None":
    """Der einzige Zonen-Lookup: kanonische Kennung oder alte Schreibweise.

    Vor 0.16.0 verglichen Entitaeten und Migrationen rohe Zeichenketten
    (``zone.zone_id == zone_id``).  Sobald eine Kennung historisch anders
    geschrieben war, fand der Raum seine Zone nicht mehr - Lesen lieferte None,
    ein Schreibvorgang wurde still verworfen.
    """
    return next((zone for zone in zones if zone_matches_room(zone, room_id)), None)


def is_schlafraum(
    *,
    room_id: object = "",
    name: object = "",
    climate_entity_id: object = "",
) -> bool:
    """True fuer die beiden Schlafraeume (Kennung ODER Name)."""
    canonical = _label_key(
        canonical_room_id(room_id, name=name, climate_entity_id=climate_entity_id)
    )
    if canonical in {alias.casefold() for alias in SCHLAFRAUM_ROOM_IDS}:
        return True
    return _label_key(canonical_zone_label(name or room_id)) in SCHLAFRAUM_LABELS


def zone_comfort_default(
    *,
    room_id: object = "",
    name: object = "",
    climate_entity_id: object = "",
) -> float:
    """Standard-Komfort-/Vorkuehlziel: 23,0 C in den Schlafraeumen, sonst 23,5 C."""
    if is_schlafraum(room_id=room_id, name=name, climate_entity_id=climate_entity_id):
        return SCHLAFRAUM_ZIEL_C
    return DEFAULT_ZONE_COMFORT_C


def zone_min_outdoor_cooling_default(
    *,
    room_id: object = "",
    name: object = "",
    climate_entity_id: object = "",
) -> float | None:
    """Standard-Aussenboden: 25,0 C in den Schlafraeumen, 20,0 C sonst oben.

    ``None`` bedeutet "kein Boden" (Erdgeschoss-Raeume).
    """
    if is_schlafraum(room_id=room_id, name=name, climate_entity_id=climate_entity_id):
        return SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C
    label = _label_key(canonical_zone_label(name or room_id))
    return DEFAULT_UPSTAIRS_MIN_OUTDOOR_C if label in UPSTAIRS_ZONE_LABELS else None


def schlafraum_house_rule_values(
    *,
    room_id: object = "",
    name: object = "",
    climate_entity_id: object = "",
) -> dict[str, float] | None:
    """Erzwungene Hausregel-Werte der beiden Schlafraeume (None = kein Schlafraum).

    Hauswunsch 23.09.2026 in der Fassung 0.16.1: fuer ``climate.schlafzimmer``
    und ``climate.kinderzimmer`` gelten

    - Komfort-/Vorkuehlziel 23,0 C (``SCHLAFRAUM_ZIEL_C``),
    - akute Kuehlgrenze 25,0 C (``SCHLAFRAUM_AKUTE_KUEHLGRENZE_C``) und
    - "Kuehlung erst ab Aussentemperatur" 25,0 C
      (``SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C``).

    Die Hausregel hat Vorrang vor einem alten Handwert aus der Zeit vor der
    Regel (z. B. 24,0 C): Die Migration in ``controller._house_zones`` setzt
    diese drei Werte fuer die beiden Raeume durch, alle uebrigen Felder und
    alle uebrigen Raeume bleiben unberuehrt.
    """
    if not is_schlafraum(room_id=room_id, name=name, climate_entity_id=climate_entity_id):
        return None
    return {
        "comfort_temperature": SCHLAFRAUM_ZIEL_C,
        "acute_cooling_limit_c": SCHLAFRAUM_AKUTE_KUEHLGRENZE_C,
        "min_outdoor_cooling_temperature_c": SCHLAFRAUM_VORKUEHL_AB_AUSSEN_C,
    }


def zone_pilot_min_default(
    *,
    room_id: object = "",
    name: object = "",
    climate_entity_id: object = "",
) -> float | None:
    """Standard-Untergrenze des Geraetesolls.

    In den Schlafraeumen ist das Kuehlziel 23,0 C - das Geraet wird also nicht
    mehr auf 22,0 C heruntergezogen.
    """
    if is_schlafraum(room_id=room_id, name=name, climate_entity_id=climate_entity_id):
        return SCHLAFRAUM_ZIEL_C
    return None


def _label_slug(text: object) -> str:
    """Entitaets-ID-Slug eines Raumnamens (dieselbe Form wie Home Assistant).

    Bewusst ohne Alias-Aufloesung: hier soll die *alte* Schreibweise sichtbar
    bleiben, sonst laesst sich eine alte Entitaets-ID nicht erkennen.
    """
    return re.sub(r"[^a-z0-9]+", "_", _label_key(text)).strip("_")


def legacy_entity_id_rename(entity_id: object) -> str | None:
    """Neue Entitaets-ID ohne alte Raumschreibweise (``None`` = nichts zu tun).

    Home Assistant bildet die Entitaets-ID einmalig aus dem Anzeigenamen.  Fuer
    den Schlafzimmer-Raum entstand sie mit dem damaligen Tippfehler, z. B.
    ``number.pv_klimaregler_schlafzimmrt_komforttemperatur``.  Der Name ist
    laengst korrigiert, die ID blieb - dadurch war der Raum unter seinem
    richtigen Namen weder lesbar (None) noch schreibbar.
    """
    domain, _, object_id = str(entity_id or "").partition(".")
    if not domain or not object_id:
        return None
    updated = object_id
    for legacy, canonical in ZONE_LABEL_ALIASES.items():
        legacy_slug, canonical_slug = _label_slug(legacy), _label_slug(canonical)
        if not legacy_slug or legacy_slug == canonical_slug:
            continue
        updated = re.sub(
            rf"(^|_){re.escape(legacy_slug)}(_|$)",
            rf"\g<1>{canonical_slug}\g<2>",
            updated,
        )
    if updated == object_id:
        return None
    return f"{domain}.{updated}"



@dataclass(frozen=True, slots=True)
class ZoneConfig:
    """A user-selected, never inferred zone mapping."""

    zone_id: str
    name: str
    climate_entity_id: str
    temperature_entity_id: str
    comfort_temperature: float = 23.5
    hard_max_temperature: float = 25.5
    pilot_min_target_temperature: float | None = None
    pilot_max_target_temperature: float | None = None
    hard_limit_failsafe_offset_c: float = 1.0
    cooling_power_entity_id: str | None = None
    priority: int = 50
    modulation_priority: int = 50
    pilot_enabled: bool = True
    minimum_plausible_temperature_c: float = 5.0
    maximum_plausible_temperature_c: float = 50.0
    use_climate_temperature_fallback: bool = False
    # Absolute air-temperature guard: at or above this measured room air the
    # controller cools even against mild-day / rain / equilibrium holds.
    # None means "use the legacy comfort + 0.3 K margin".
    acute_cooling_limit_c: float | None = None
    # Outdoor floor for cooling (upstairs rule): below this outdoor
    # temperature the room is not cooled at all - only the hard limit stays.
    min_outdoor_cooling_temperature_c: float | None = None
    # Draft-sensitive zone: the controller forces the quiet fan stage on its
    # own commands.  Sleep/child rooms may disable this and let the device
    # automatic fan modulation run (2026-09-06 household request).
    quiet_fan: bool = True
    # Look-ahead of this room's temperature forecast, in minutes.  The glass
    # living room heats quickly (+0.43 C/h measured), so it can look two hours
    # ahead and start its pre-cool step earlier instead of reacting at the
    # comfort limit (household request 2026-09-20: "im Wohnzimmer besonders
    # praediktiv arbeiten").
    forecast_horizon_minutes: float = 60.0
    # PV hold mode: while real PV surplus is available the room is kept on a
    # constant level instead of switching the unit off as soon as comfort is
    # reached.  Stored as an ABSOLUTE temperature on the room's own sensor
    # scale, so the dashboard shows two comparable numbers ("Komfort 24,0" and
    # "Pegel 23,5") instead of a delta that invites arithmetic and looks like a
    # contradiction (household feedback 2026-09-20: "woher kommen die 23,5?
    # im dashboard ist komfort auf 24").  0 = off.
    # Household decision 2026-09-20: hold with PV only, never on grid power.
    hold_level_c: float = 0.0
    # Kombi-Logik (Hauswunsch 2026-09-21): optionale zweite Temperaturquelle
    # (z. B. das Mittel der Loxone-Taster im Wohnzimmer).  Leer = aus; dann
    # regelt der Raum unverändert auf seiner eigenen Quelle (Luft).
    blend_entity_id: str = ""
    blend_weight_pct: float = 40.0
    # Anteil der Zweitquelle in Prozent (0 = aus, 40 = Hausstandard).  Die Luft
    # bleibt immer die Mehrheit; bei fehlender/unplausibler Zweitquelle fällt
    # die Regelung still auf die Luft zurück (siehe blend.py).
    shade_entity_ids: tuple[str, ...] = ()
    facade_azimuths: tuple[float, ...] = ()
    facade_shade_entity_ids: tuple[tuple[str, ...], ...] = ()
    overhang_cutoff_elevation: float | None = None

    def __post_init__(self) -> None:
        """Keep legacy typos out of every customer-facing room label."""
        object.__setattr__(self, "name", canonical_zone_label(self.name))


@dataclass(frozen=True, slots=True)
class ZoneInput:
    """Validated input required for one evaluation."""

    temperature_c: float | None
    climate_available: bool
    manual_override: bool = False
    temperature_source: str = "external_sensor"


@dataclass(frozen=True, slots=True)
class ZoneForecast:
    """A conservative, read-only temperature outlook for a room."""

    zone_id: str
    trend_c_per_h: float | None
    predicted_temperature_60m_c: float | None
    sample_count: int
    data_quality: str


@dataclass(frozen=True, slots=True)
class ThermalResponse:
    """Learned room response from observed, uncommanded operation."""

    passive_trend_c_per_h: float | None
    cooling_trend_c_per_h: float | None
    observed_cooling_effect_c_per_h: float | None
    passive_sample_count: int
    cooling_sample_count: int


@dataclass(frozen=True, slots=True)
class ThermalProfile:
    """Contextual, observed room behaviour; never a simulated result."""

    passive_sun_trend_c_per_h: float | None
    passive_shaded_trend_c_per_h: float | None
    cooling_trend_c_per_h: float | None
    passive_sun_samples: int
    passive_shaded_samples: int
    cooling_samples: int
    data_quality: str


@dataclass(frozen=True, slots=True)
class ZoneDecision:
    """Recorder-friendly result of one zone evaluation."""

    zone_id: str
    state: ZoneState
    demand: bool
    score: float
    requested: bool
    reason_code: str
    reason_text: str
    strategy: str = "standard"
    recommended_target_temperature_c: float | None = None


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    """Global settings required in Gate C."""

    shadow_mode: bool
    energy_policy: EnergyPolicy
    living_room_pilot_enabled: bool = False
    zone: ZoneConfig | None = None
    ems_granted_stages_entity_id: str | None = None
    ems_stale_after_s: float = 300.0
    pv_power_entity_id: str | None = None
    export_power_entity_id: str | None = None
    export_power_positive: bool = True
    pv_forecast_power_entity_id: str | None = None
    outdoor_unit_power_entity_id: str | None = None
    heat_pump_priority_entity_id: str | None = None
    heat_pump_power_entity_id: str | None = None
    min_pv_surplus_w: float = 1000.0
    no_pv_hold_max_power_w: float = 350.0
    house_zones: tuple[ZoneConfig, ...] = ()
    outdoor_temperature_entity_id: str | None = None
    cooling_start_offset_c: float = 0.7
    mild_outdoor_comfort_temperature: float = 25.0
    hot_outdoor_comfort_temperature: float = 24.0
    living_evening_comfort_temperature: float = 24.5
    living_evening_start_time: str = "20:30"
    living_evening_end_time: str = "23:30"
    weather_forecast_entity_id: str | None = None
    outdoor_relaxation_band_c: float = 1.5
    outdoor_no_active_cooling_c: float = 0.5
    outdoor_rain_hold_probability_pct: float = 60.0
    outdoor_pv_boost_extra_w: float = 2000.0
    solar_irradiance_entity_id: str | None = None
    sun_entity_id: str | None = None
    bedroom_mode_enabled: bool = True
    bedroom_cutoff_enabled: bool = True
    bedroom_start_time: str = "15:30"
    child_bedroom_start_time: str = "15:30"
    bedroom_cutoff_time: str = "18:30"
    bedroom_quiet_enabled: bool = True
    bedroom_quiet_time: str = "18:30"
    bedroom_target_temperature: float = 22.5
    manual_override_enabled: bool = True
    v2_shadow_enabled: bool = False
    v2_house_control_enabled: bool = False
    v2_vacation_entity_id: str | None = None
    v2_cooling_season_entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class EMSGrant:
    """Validated capacity response from any external EMS."""

    stages: int
    available: bool
    reason_code: str
    reason_text: str


@dataclass(frozen=True, slots=True)
class EnergySnapshot:
    """Normalized, read-only energy values used for diagnostics."""

    pv_power_w: float | None = None
    export_power_w: float | None = None
    pv_forecast_power_w: float | None = None
    outdoor_unit_power_w: float | None = None
    heat_pump_power_w: float | None = None
