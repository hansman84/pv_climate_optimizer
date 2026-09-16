# V1-Entfernung – Inventar und Plan (Stand 0.4.60)

V1 (der ursprüngliche „PV-Pilot") ist seit Wochen **nicht mehr aktiv**: Alle
Pilot-Schalter sind aus, V1 hat Schreibverbot (`v1_may_write = false`), und
die Pilotentscheidungs-Sensoren melden „Pilot ist in der GUI ausgeschaltet".
V2 ist der einzige Regler. Ziel: **ein einziger Regelpfad**, weniger Entities,
weniger Verwirrung – ohne Sicherheitsverlust.

## 1. Was V1 noch schreibt (heute)

* Nichts. Belege:
  * `switch.pv_klimaregler_*_pilot_aktiv` (6 Stück): seit 01.09. durchgehend `off`
  * `sensor.pv_klimaregler_*_pilotentscheidung`: „…-Pilot ist in der GUI ausgeschaltet"
  * `async_apply_pilot_action()` bricht bei `not authority.v1_may_write` mit
    `authority_blocked` ab – solange V2 das Haus führt, geht kein Befehl raus.
* Der V1-„Fallback" (`failback_v2_to_v1`) fällt damit auf einen **abgeschalteten**
  Regler zurück, also faktisch auf „kein Regler". Kein Sicherheitsgewinn.

## 2. Entfernbar (V1-Kern)

| Bereich | Datei / Symbol | Umfang |
|---|---|---|
| Pilot-Engine | `pilot.py` (`PilotAction`, `LivingRoomPilot`, `living_room_pilot_eligible`) | 1070 Zeilen |
| Controller | `decide_living_room_pilot`, `decide_office_pilot`, `decide_speis_pilot`, `decide_bedroom_pilot`, `_ensure_bedroom_pilots`, `async_apply_pilot_action`, `set_*_pilot_enabled`, `request_*_pilot_takeover`, `failback_v2_to_v1`, `begin/complete_v1_rollback`, `v1_may_write`-Zweige | ~500 Zeilen |
| Integration | Pilot-Aufrufe in `__init__.py` (4 Stellen), `_pilot_service_executor` (nur umbenennen – V2 nutzt denselben Executor) | ~80 Zeilen |
| Entities | `ZonePilotSwitch` (5), `LivingRoomPilotSwitch` (1 + 1 Duplikat), `LivingRoom/Office/SpeisPilotTakeoverButton` (3), `PilotActionSensor` + `Office/Speis/BedroomPilotActionSensor` (4) | ~13 Entities |
| Tests | Großteil von `tests/test_pv_climate_controller.py` (2839 Zeilen, 134 Tests, davon 361 Pilot-Bezüge) | ~2000 Zeilen |
| config_flow | Pilot-Optionen (`living_room_pilot_enabled`, Zone-Pilot-Gates) | klein |

## 3. Muss bleiben (V2 nutzt es)

| Baustein | Grund |
|---|---|
| `command_adapter.py` | Rate-Limit, Ein-Befehl-pro-Minute, **Manuell-Override-Erkennung** – von V2 genutzt |
| `v2_authority.py` | Single-Writer-Guard; V1-Zustände (`V1_ACTIVE`, `rollback_pending`) entfallen, `V2_ACTIVE`/`SHADOW` bleiben |
| `pilot_min_target_temperature` / `pilot_max_target_temperature` (je Zone) | V2 liest sie als **Gerätesoll-Grenzen** (u. a. „entspannte Stufe" bei Hold/Auslauf). Entity-IDs behalten, **Friendly Name** auf „Gerätesoll min/max" ändern |
| EMS-/Granted-Stages-Entity (`CONF_EMS_GRANTED_STAGES_ENTITY_ID`) | Energiepolitik/Budget (`house.py`) |
| `ManualOverrideSwitch`, `RoomManualTakeoverReleaseButton` | Handeingriff-Schutz; prüfen, ob V2 sie liest – dann behalten, sonst entfernen |

## 4. Nacharbeiten außerhalb des Codes

* Dashboard **Klima-Steuerzentrale** (`klima-steuerung`): enthält 6 Pilot-Schalter,
  3 Pilotentscheidungs-Sensoren und vermutlich die Takeover-Buttons → entfernen.
* Label „V2-Pilot" in `klima-control` meint `switch.*_v2_steuerung` → auf „V2-Steuerung" umbenennen.
* HA-Helper `input_number.wohnzimmer_pilot_ems_freigabe`: prüfen, ob als EMS-Entity
  konfiguriert. Falls nein: löschen.
* YAML-Automationen/Skripte sind per API nicht lesbar → einmal manuell nach
  „pilot" suchen (Einstellungen → Automationen/Skripte).

## 5. Ersatz für den Fallback (statt V1)

Neu, klein und sicher – **Safe-Hold**:
1. V2-Kommando schlägt fehl → Kommando verwerfen (kein Retry-Sturm)
2. Gerät auf **entspanntes Soll** anheben (wie Gate-Hold/Wind-down, 0.4.60)
3. Sensor „V2-Kommunikationsfehler" + Push-Nachricht an den Nutzer
4. Kein Zweitregler, keine konkurrierenden Schreiber

Umfang ~50 Zeilen + Tests.

## 6. Aufwand & Risiko

* Aufwand: ~1 Arbeitstag inkl. Tests und Doku
* Risiko: mittel, aber gut abgrenzbar (viele alte Tests werden gelöscht)
* Vorgehen: eigener Branch `refactor/remove-v1`, Suite grün, dann Release 0.5.0
* Ergebnis: ~13 Entities und ~3000 Code-/Testzeilen weniger, **ein** Regelpfad
