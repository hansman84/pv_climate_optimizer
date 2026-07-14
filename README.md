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

## Familien-Komfortprofil (Shadow Mode)

Der Raumplan zeigt zusätzlich zu Messwerten eine **Strategie** und eine
empfohlene Zieltemperatur. Das ist absichtlich noch keine Geräteansteuerung;
es macht die künftige Regelentscheidung zuerst beobachtbar.

| Raumgruppe | Zeitfenster | Shadow-Empfehlung |
| --- | --- | --- |
| Wohnzimmer | 07:00–22:00 | Komforttemperatur (derzeit 23,5 °C) bewerten |
| Wohnzimmer | 22:00–07:00 | Kein Komfortkühlen; nur die harte Raumgrenze schützen |
| Schlaf- und Kinderzimmer | 15:00–21:00 | Bei PV-Mindestüberschuss auf höchstens 23 °C vorkühlen |
| Schlaf- und Kinderzimmer | 21:00–07:00 | Bei mehr als 23 °C Schlafziel als Kühlbedarf ausweisen |
| Schlaf- und Kinderzimmer | übrige Tageszeit | Kein Komfortziel; die harte Grenze bleibt immer aktiv |

Der PV-Überschuss ist erreicht, wenn die konfigurierte Netzeinspeisung den
`PV-Mindestüberschuss` erreicht. Die Attribute jedes Raum-Shadow-Plans
enthalten `strategy`, `recommended_target_temperature_c`, `reason_code` und
die gemessene thermische Entwicklung. Damit lässt sich die Logik mit realen
Familienabläufen prüfen, bevor überhaupt eine ConnectLife-Anbindung in
Betracht kommt.

## Beschattung pro Fassade

Eine Fassade kann mehrere Rolläden enthalten. Für breite Schiebetüren werden
beide Teilrolläden in **derselben** Auswahl „Fassade n – alle Teilrollläden“
gewählt. Ihr Mittelwert wird als gemeinsamer Beschattungsgrad dieser Fassade
verwendet. Azimut und Teilrollladengruppe werden als Paar gespeichert; eine
leere frühere Fassade verschiebt die Zuordnung nicht.

## Entwicklung

```bash
pytest -q tests/test_pv_climate_controller.py
python3 -m compileall -q custom_components/pv_climate_controller
```

## Sicherheit

- Kein automatisches Einschalten oder Ausschalten von Klimageraeten.
- Fehlende, ungueltige oder veraltete EMS-Freigaben sperren fail-safe.
- Rate-Limits und Entscheidungsgruende sind nachvollziehbar sichtbar.

## Multi-Split-Hausmodell

Die installierte Außenanlage ist eine Hisense `5AMW125U4RTA` mit fünf
Anschlüssen. Ihr konservatives gemeinsames Kühlbudget beträgt 12,5 kW
(rund 42.650 BTU/h); die im Datenblatt genannte modulierte Maximalleistung
von 15,3 kW ist kein Regel-Sollwert. Die Leistungssensoren der Innengeräte
werden als Beobachtung der tatsächlich abgegebenen Kühlleistung addiert.

Für eine spätere Hausfreigabe gelten feste Sicherheitsregeln: höchstens fünf
Zonen, keine automatische Empfehlung bei gleichzeitig beobachtetem Heiz- und
Kühlbetrieb, und keine automatische Änderung von `fan_mode`, `swing_mode`,
`dry`, `fan_only` oder `auto`. Diese Betriebsarten bleiben Bedien- bzw.
Komfortfunktionen; die temperaturgeführte Planung verwendet ausschließlich
eindeutige Kühl- oder Heizanforderungen.

Die bisherige Hisense-LAN-Untersuchung bleibt als
[`docs/hisense_local_poc.md`](docs/hisense_local_poc.md) erhalten und ist
nicht Teil des HACS-Releases.

Die technische Datenverarbeitung, die Raum-Shadow-Pläne und die
Sicherheitsinvarianten sind in [`docs/architecture.md`](docs/architecture.md)
beschrieben. Die laufende Bedienung steht in
[`docs/OPERATING_GUIDE.md`](docs/OPERATING_GUIDE.md).
