# Technische Architektur

## Ziel und harte Grenze

PV Climate Controller ist derzeit ein **Mehrzonen-Planer im Shadow Mode**.
Er beobachtet ausschließlich bereits konfigurierte Home-Assistant-Entities und
stellt Ergebnisse als eigene Entities bereit. Die Integration enthält keinen
Executor für `climate`-Services und verändert weder Klimageräte noch bestehende
Automationen. Auch ein deaktivierter Shadow Mode schaltet keine produktive
Steuerung frei.

## Datenfluss

```text
explizit konfigurierte Quellen
  Temperatur / Climate-Modus / optionale BTU/h / PV / Einspeisung / Prognose
                                  |
                                  v
                       Validierung und Normierung
                                  |
                                  v
     Raumentscheidung (Komfort, harte Grenze, Verfügbarkeit, Priorität)
                                  |
                                  v
       Hausplan der gemeinsamen Außenanlage, nur als Diagnose
                                  |
                                  v
  Sensoren: Raum-Shadow-Plan, Haus-Kühlplan, PV-Entscheidung und Leistung
```

Eine Quelle wird nie aus Namen, Bereichen oder ähnlichen Entities abgeleitet.
Ungültige, unbekannte und nicht verfügbare Werte werden als fehlend behandelt.

## Mehrzonenmodell

Jede Zone besitzt eine explizite Klima-Entity, einen Temperatursensor,
Komforttemperatur, harte Obergrenze, Priorität und optional einen
BTU/h-Leistungssensor. Die Raumentscheidung ist unabhängig von der PV-Lage:
Sie beschreibt ausschließlich den thermischen Bedarf. Die PV-Entscheidung
beschreibt getrennt, wie die gewählte Energiepolitik diesen Bedarf bewerten
würde. Damit bleiben Komfort- und Energierisiko sichtbar statt vermischt.
Der Hausplan führt diese Ergebnisse mit `energy_permits_cooling` und
`energy_reason` zusammen. Das ist eine transparente Empfehlung, keine
Freigabe und kein Gerätebefehl.

Der Hausplan verwendet für die Hisense 5AMW125U4RTA ein konservatives,
gemeinsames Nenn-Kühlbudget von 12,5 kW (ca. 42.652 BTU/h) und maximal fünf
Innengeräte. BTU/h werden nur bei beobachtetem `cool` oder `dry` addiert.
`auto` wird bewusst nicht als Kühlung interpretiert, da es auch Heizen wählen
kann. Gleichzeitiges Heizen und Kühlen, zu viele Zonen oder ein überschrittenes
Nennbudget unterdrücken jede Empfehlung.

## Entities und Nachvollziehbarkeit

`sensor.pv_klimaregler_haus_kuhlplan` enthält eine vollständige,
Recorder-taugliche Liste der Raumpläne. Für jede konfigurierte Zone gibt es
zusätzlich einen eigenen Sensor `…_shadow_plan`. Dessen Attribute enthalten
Temperatur, Modus, Verfügbarkeit, Priorität, beobachtete BTU/h, Bedarf, Score
und Reason-Code. Diese Werte sind für Dashboard, Verlauf und Fehlersuche
gedacht; sie sind keine Befehlswarteschlange.

Zusätzlich erzeugt jede Zone eine **Temperaturprognose** für 60 Minuten. Sie
nutzt ausschließlich den lokalen Verlauf seit dem letzten Neustart und bleibt
leer, bis mindestens zwei valide Messpunkte vorliegen. Werte außerhalb von
5–50 °C werden als Datenqualitätsproblem markiert und können keinen
Kühlbedarf auslösen. Das schützt insbesondere vor ausgefallenen Sensoren, die
statt `unavailable` einen Platzhalterwert liefern.

Für jeden Raum kann optional die `current_temperature` der bereits explizit
ausgewählten Klima-Entity als Fallback aktiviert werden. Sie wird nur verwendet,
wenn der externe Sensor fehlt oder unplausibel ist; der Raumplan weist die
genutzte Temperaturquelle dabei aus.

## Zonenverwaltung

Unter **Konfigurieren** der Integration lassen sich Zonen hinzufügen,
bearbeiten oder entfernen. Entfernen löscht ausschließlich die Zuordnung im
Regler. Alle Entity-Auswahlen sind Selector-Felder, damit keine IDs geraten
werden. Ein Raum kann nicht doppelt dieselbe Klima-Entity verwenden, und die
harte Grenze darf nicht unter der Komforttemperatur liegen.

## Prüfung

- `python3 -m compileall -q custom_components/pv_climate_controller`
- `pytest -q tests/test_pv_climate_controller.py`
- `git diff --check`
- Suche nach Climate-Service-Aufrufen; jeder Fund ist vor einem Release zu
  untersuchen.
