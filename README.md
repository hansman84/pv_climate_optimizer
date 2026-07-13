# PV Climate Optimizer

Home-Assistant-Custom-Integration zur sicheren, nachvollziehbaren
PV-orientierten Bewertung vorhandener Klimageraete.

Der Controller startet immer im **Shadow Mode**. Er wertet Temperatur,
PV-/EMS-Signale und Schutzbedingungen aus, erzeugt aber keine direkten
Climate-Service-Aufrufe. Potenzielle Befehle bleiben Diagnosedaten, bis eine
separate produktive Freigabe implementiert und getestet wurde.

## HACS-Installation

1. In HACS unter **Custom repositories**
   `hansman84/pv_climate_optimizer` als Typ **Integration** hinzufuegen.
2. Die Integration herunterladen und Home Assistant neu starten.
3. Unter **Einstellungen > Geraete & Dienste** `PV Climate Optimizer`
   hinzufuegen.
4. Den ersten Pilot im Shadow Mode konfigurieren und die Diagnose-Entities
   beobachten.

## PV-Werte im Dashboard

Unter **Konfigurieren** der Integration lassen sich drei vorhandene Sensoren
auswaehlen: aktuelle PV-Leistung, Netzeinspeisung und PV-Prognose. Die
Integration zeigt sie als eigene Diagnose-Entities in Watt an und aktualisiert
sie bei jeder Aenderung der gewaehlten Quelle.

- **Netzeinspeisung ist positiv** legt die Vorzeichenkonvention der gewaehlten
  Quelle fest. Ist Einspeisung dort negativ, den Schalter deaktivieren.
- **PV-Mindestueberschuss** ist die Grenze fuer `PV-Ueberschuss verfuegbar`.
  Das ist derzeit eine reine Shadow-Mode-Diagnose und schaltet kein Geraet.
- Nicht konfigurierte oder ungueltig dimensionierte Quellen bleiben leer,
  statt einen Wert zu erfinden.

## Entwicklung

```bash
pytest -q tests/test_pv_climate_controller.py
python3 -m compileall -q custom_components/pv_climate_controller
```

## Sicherheit

- Kein automatisches Einschalten oder Ausschalten von Klimageraeten.
- Fehlende, ungueltige oder veraltete EMS-Freigaben sperren fail-safe.
- Rate-Limits und Entscheidungsgruende sind nachvollziehbar sichtbar.

Die bisherige Hisense-LAN-Untersuchung bleibt als
[`docs/hisense_local_poc.md`](docs/hisense_local_poc.md) erhalten und ist
nicht Teil des HACS-Releases.
