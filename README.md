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
