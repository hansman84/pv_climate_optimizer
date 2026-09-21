"""Kombi-Logik: Regelgröße aus zwei Temperaturquellen (Hauswunsch 2026-09-21).

Das Wohnzimmer hat zwei Messquellen mit unterschiedlichem Charakter:

* die **Luft** (IKEA-AirQ) - schnell, aber unruhig (+-0,8 K in 30 Minuten), und
* das **Taster-Mittel** des Loxone-Raumreglers (``tempActual``) - träge, aber
  deutlich ruhiger (Standardabweichung rund ein Drittel der Luft) und rund
  0,8 K wärmer, weil die Taster an der Wand sitzen und Strahlungswärme mitnehmen.

Das Haus empfindet die Wahrheit als "irgendwo in der Mitte".  Physikalisch ist
das die **operative Temperatur**: bei ruhender Luft etwa 60 % Luft und 40 %
Strahlungswirkung der umgebenden Flächen (ISO 7726).  Diese Funktion bildet das
ab - mit zwei harten Schutzregeln, damit eine kaputte Zweitquelle nie die
Regelung verdirbt:

1. Die Zweitquelle muss **frisch** sein (Standard: 60 Minuten).
2. Sie darf nicht **unplausibel** weit von der Luft abweichen (Standard: 2,5 K).

In beiden Fällen regelt der Raum unverändert auf der Luft - die Kombi fällt
still auf den bewährten Wert zurück.
"""

from __future__ import annotations

from dataclasses import dataclass

# Defaults des Haushalts (am Dashboard als "Zweitquelle-Anteil (%)" einstellbar).
DEFAULT_BLEND_WEIGHT_PCT = 40.0
MAX_BLEND_WEIGHT_PCT = 70.0
DEFAULT_MAX_AGE_S = 60 * 60.0
DEFAULT_MAX_DEVIATION_C = 2.5
# 0.10.0: Deckel für den Zuschlag der Zweitquelle.  Ein Split-Geraet kuehlt nur
# die Luft, die Wand folgt traege - ohne Deckel wird das gemischte Ziel
# unerreichbar und die Anlage laeuft endlos.  1.0 K heisst: die Luft geht nie
# mehr als 1.0 K unter das eingestellte Ziel, das Gefuehl stimmt trotzdem.
DEFAULT_MAX_OFFSET_C = 1.0


@dataclass(frozen=True, slots=True)
class BlendResult:
    """Ergebnis einer Mischrechnung, immer mit Begruendung."""

    value_c: float | None
    reason: str
    second_used: bool
    weight_pct: float
    offset_c: float = 0.0
    capped: bool = False


def blend_source_candidates(states: object, *, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Plausible Temperatursensoren als Auswahlliste für die Zweitquelle.

    Hauswunsch 2026-09-21: \"ich will im dashboard die entity einfach umstellen
    können\" - eine Auswahlliste ist auf dem Telefon ein Tipp statt Tipparbeit.
    Aufgenommen wird jeder Sensor mit Einheit °C, der weder zur Integration
    selbst gehört noch ausgeschlossen wurde.  Sortiert nach Entity-ID.
    """
    found: list[str] = []
    for state in states:  # type: ignore[union-attr]
        entity_id = getattr(state, "entity_id", "")
        attributes = getattr(state, "attributes", {}) or {}
        if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
            continue
        if entity_id in exclude or "pv_klimaregler" in entity_id:
            continue
        if attributes.get("unit_of_measurement") not in {"°C", "C", "°C"}:
            continue
        if getattr(state, "state", None) in {"unknown", "unavailable", None, ""}:
            continue
        found.append(entity_id)
    return tuple(sorted(set(found)))


def blend_room_temperature(
    primary_c: float | None,
    second_c: float | None,
    weight_pct: float | None = DEFAULT_BLEND_WEIGHT_PCT,
    *,
    second_age_s: float | None = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    max_deviation_c: float = DEFAULT_MAX_DEVIATION_C,
    max_offset_c: float | None = DEFAULT_MAX_OFFSET_C,
) -> BlendResult:
    """Kombiniere Luftwert und Zweitquelle zur Regelgröße des Raums.

    ``weight_pct`` ist der Anteil der Zweitquelle in Prozent (0 = aus, 40 =
    Hausstandard, maximal 70 - die Luft bleibt immer die Mehrheit).
    """
    weight = float(weight_pct if weight_pct is not None else DEFAULT_BLEND_WEIGHT_PCT)
    weight = max(0.0, min(MAX_BLEND_WEIGHT_PCT, weight))

    if primary_c is None:
        return BlendResult(None, "no_primary_temperature", False, weight)
    primary = float(primary_c)

    if weight <= 0.0:
        return BlendResult(primary, "blend_off", False, 0.0)
    if second_c is None:
        return BlendResult(primary, "second_source_missing", False, weight)
    if second_age_s is not None and second_age_s > max_age_s:
        return BlendResult(primary, "second_source_stale", False, weight)
    second = float(second_c)
    if abs(second - primary) > max_deviation_c:
        return BlendResult(primary, "second_source_implausible", False, weight)

    offset = (weight / 100.0) * (second - primary)
    capped = False
    if max_offset_c is not None and abs(offset) > float(max_offset_c):
        offset = float(max_offset_c) if offset > 0.0 else -float(max_offset_c)
        capped = True
    blended = primary + offset
    return BlendResult(round(blended, 2), "blended", True, weight, round(offset, 2), capped)
