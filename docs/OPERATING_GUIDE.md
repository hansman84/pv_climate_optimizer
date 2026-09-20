# PV Klimaregler – Betrieb

## Sicherheitsmodell

Die Integration ist ein Mehrzonen-Shadow-Controller. Sie liest PV-Leistung,
Netzeinspeisung, Temperatur, Betriebsmodus und beobachtete Kühlleistung der
Innengeräte. Sie ruft keine `climate`-Services auf und ändert keine bestehenden
Automationen.

## Hausmodell

Die Hisense 5AMW125U4RTA versorgt maximal fünf Innengeräte. Der Hausplan nutzt
12,5 kW als konservatives gemeinsames Kühlbudget. Die pro Zone gemeldeten
BTU/h-Werte werden zu einer beobachteten Gesamtleistung addiert. Das Modell
berücksichtigt keine automatische Freigabe bei gleichzeitigem Heizen und
Kühlen, bei mehr als fünf Zonen oder bei überschrittenem Nennbudget.

## Zonen anlegen

Unter **Einstellungen → Geräte & Dienste → PV Klimaregler → Konfigurieren**:

1. **Zone hinzufügen** öffnen.
2. Raumname, Klima-Entity und Temperatursensor auswählen.
3. Optional die Kühlleistung in BTU/h auswählen.
4. Komforttemperatur, harte Temperaturgrenze und Priorität 1–100 setzen;
   höher bedeutet bei gleicher Temperaturdringlichkeit zuerst im Shadow-Plan.

Der bestehende Wohnzimmer-Pilot wird beim ersten Hinzufügen als Hauszone
übernommen. Neue Zonen verwenden 23,5 °C und 25,5 °C als Standard. Über
**Zonen bearbeiten oder entfernen** lassen sich alle Zuordnungen, Grenzwerte
und Prioritäten später ändern. Das Entfernen löscht nur die Zuordnung des
Reglers; Klima-Entity und Automationen bleiben unverändert.

## Betriebsarten

`cool` und `heat` sind temperaturrelevante Betriebsarten. `fan_only`, `dry`,
`auto`, Lüfterstufe und Swing werden sichtbar beobachtet, aber nicht verändert.
So bleiben Lautstärke, Luftführung und Entfeuchtung unter manueller Kontrolle.
Für das gemeinsame Leistungsbudget werden nur beobachtete BTU/h in `cool` oder
`dry` summiert; `auto` wird nicht als Kühlung angenommen.

## Wohnzimmer: nur noch 3 Regeln (0.5.9)

Auf Wunsch aufgeräumt — die früheren WZ-Sonderregeln sind **entfernt**:

- ❌ „Entspannen auf die Auslaufstufe" (Gate-Hold hat das Soll auf 25 °C gehoben) → hat mit dem Komfort-Soll um denselben Wert gestritten
- ❌ „PV-Boost" (extra −1 K-Stufe als Ziel)
- ❌ „Telemetrie-Ersatz" (Wechselrichterdaten fehlen + Sonne → trotzdem kühlen)
- ❌ „Wohnzimmer-Priorität" (Start ohne stabile PV-Reserve)
- ❌ Sonderfall „sonniger Tag ohne Einspeisung"

**Es bleiben genau drei Regeln** (wie in jedem anderen Raum):
1. **Komforttemperatur** (Soll; Start zielt immer auf Komfort, nie auf den Deckel)
2. **Akute Kühlgrenze** (Notausgang, kühlt auch ohne PV)
3. **Harte Temperaturgrenze** (26 °C Dead-End)

Voraussetzung für normale Starts ist **echter PV-Überschuss** (≥ Schwelle, 3 Min stabil).
Zusätzlich gilt seit 0.5.8 ein **Kurzzyklus-Schutz**: nach jedem Ausschalten 10 Min Pause
(Notfälle ausgenommen).

## V1 entfernt (0.5.0)

Der alte **V1-Pilot** ist vollständig entfernt: `pilot.py`, die `decide_*_pilot`-
Pfade, Pilot-Schalter/-Buttons/-Sensoren sowie der V1-Failback existieren nicht
mehr. V2 ist der **einzige** Reglerpfad; es gibt genau einen Schreiber.

Statt `failback_v2_to_v1` gilt jetzt **Safe-Hold**: Schlägt ein V2-Kommando am
Transport fehl, wird es verworfen (kein Retry-Sturm), das betroffene Gerät
best-effort auf die entspannte Sollstufe (`<Zone> – Gerätesoll max`) angehoben
und der Fehler pro Zone gezählt (`note_v2_transport_failure`).

Die früheren „Pilot"-Zahlen sind reine **Gerätegrenzen** und werden von V2
gelesen: `Gerätesoll min` / `Gerätesoll max` je Zone.

## Haushalts-Regler (V2, Stand 0.4.58)

Auf der Dashboard-Detailseite jedes Raums liegt der Block **Kühl-Logik** mit
zwei editierbaren Zahlen. Beide sind Haushaltsentscheidungen, keine
Technikwerte – sie verschieben nur, *wann* V2 kühlen darf:

1. **Akute Kühlgrenze (Standard: Komforttemperatur + 0,9 K).** Steigt die
   echte Raumluft auf oder über diesen Wert, kühlt V2 auch dann, wenn der Tag
   mild, eine Regenstrecke aktiv oder das Außenluft-Gleichgewicht erreicht
   ist. Kleiner = kühler/mehr Laufzeit, größer = sparsamer/wärmer.
2. **Kühlung erst ab Aussentemperatur (Default Obergeschoss 20 °C,
   Wohnzimmer/Speis 0 = aus).** Liegt die Außentemperatur darunter, wird der
   Raum nicht gekühlt – gedacht für Schlaf-, Kinder- und Spielzimmer, damit an
   kühlen Tagen nicht gegen eine laufende Heizung gekühlt wird. `0` schaltet
   die Regel ab.

**Dead-End:** Die harte Temperaturgrenze (z. B. 26 °C) übersteuert die
weichen Regeln eines Raums (Außengrenze, Ruhezeit, PV-Holds) – aber **nur
solange Kühlen eingeschaltet ist**. Ist der Kühlsaison-Schalter aus (oder
Urlaub aktiv), kühlt gar nichts, auch der Dead-End nicht (0.4.59).

**Schlafräume:** Vorkühlung (15:30 bis Ruhezeit) zielt auf die
Zonen-Komforttemperatur, nicht mehr auf das Nachtziel
(`Schlafraum-Abendzieltemperatur`, Default 22,5 °C). Die Vorkühlung war bis
0.4.57 die Ursache für „zu kalte" Schlaf-/Kinderzimmer (Gerät rundete 22,5 auf
22). Zusätzlich sichern zwei HA-Watchdogs, dass Schlaf- und Kinderzimmer nie
unter 23 °C gekühlt werden.

**Priorität der Regeln (von stark nach schwach):** Dead-End harte Grenze →
Saison-/Urlaubssperre → Außengrenze pro Raum → Ruhezeit/Bedroom-Fenster →
akute Kühlgrenze → Komfort-/PV-Logik.

## Dashboard

Das Dashboard **PV Klimaregler** hat zwei bewusst unterschiedliche Ansichten:

1. **Klima-Flow** ist die Alltagsansicht. Sie beantwortet zuerst: *Was ist
   jetzt der beste nächste Schritt?* Die thermische Landkarte darunter zeigt
   alle Räume mit Temperatur jetzt, 60-Minuten-Prognose und
   Temperaturgradient. PV-Leistung, Überschuss und PV-Prognose machen direkt
   sichtbar, ob ein gutes Kühlfenster vorliegt.
2. **Analyse & Feintuning** ist die erweiterte Leitwarte. Sie enthält den
   gemeinsamen Temperaturverlauf, Hauskapazität sowie je Raum Gradienten,
   Zeit bis zur Komfort- und harten Grenze, gelernten Kühleffekt und direkte
   Regler für Komforttemperatur, harte Grenze und Priorität.

Beide Ansichten verwenden die eigene Karte **PV Climate Command Center**. Sie
stellt Raumwerte, Energiefluss und die sicheren Planungsregler als Oberfläche
dar, statt die zugrunde liegenden Entities aufzuzählen. Die Karte speichert
nur Änderungen an den `number`, `select` und `switch`-Entities dieser
Integration. Sie ruft keine `climate`-Services auf.

`Arbeitszimmer / Spielzimmer` bezeichnet dabei den Raum Arbeitszimmer mit dem
Klimagerät Spielzimmer.

Die Raumregler in **Analyse & Feintuning** ändern ausschließlich die
Planungsgrenzen des jeweiligen Raums. Die harte Grenze wird nie unter die
Komforttemperatur gesetzt. Die Priorität (1–100) entscheidet nur bei sonst
vergleichbarer thermischer Dringlichkeit. Die Standardwerte für neu angelegte
Zonen sind 23,5 °C Komforttemperatur und 25,5 °C harte Grenze.

Der **Shadow-Plan** einer Zone enthält Temperatur, Betriebsmodus, Priorität,
BTU/h-Beobachtung und Reason-Code. Der Haus-Kühlplan fasst alle Raumpläne, die
aktive Zonenzahl, thermischen Bedarf, gemeinsames Nennbudget und die aktuelle
Prioritätsreihenfolge zusammen.

Ein positiver **Temperaturgradient** bedeutet Erwärmung pro Stunde; ein
negativer Gradient bedeutet, dass der Raum bereits abkühlt. Die Zeiten bis zu
einer Grenze werden nur bei einem belastbaren, steigenden Temperaturtrend
berechnet. Ein leerer Wert bedeutet daher nicht "unbekanntes Risiko", sondern
"keine belegte Erwärmung in Richtung dieser Grenze".

### Temperatur-Backup

Jede Zone kann optional die vom Innengerät gemeldete Temperatur als Backup
verwenden. Dieser Schalter ist nur sinnvoll, wenn der externe Raumfühler
ausfällt oder offensichtliche Fehlwerte liefert. Er ersetzt den externen Fühler
nur bei fehlenden oder unplausiblen Werten und sendet keinen Befehl an das
Klimagerät. Eine Temperatur unter 5 °C oder über 50 °C gilt als unplausibel.

Die Temperaturprognose bleibt leer, bis mindestens zwei plausible Messpunkte
seit dem Start des Reglers vorhanden sind. Das ist absichtlich konservativ:
Eine fehlende Prognose ist keine erfundene Schätzung.

## Produktiver Pilot

Ein produktiver Pilot ist ausdrücklich nicht enthalten. Vor einer späteren
Freigabe müssen Shadow-Plan, Betriebszustände und Hausbudget über reale
Szenarien geprüft und separat abgenommen werden.
# V2.1 local review boundary

V2.1 is reviewed locally. Use `pytest -q`, `python3 -m compileall -q
custom_components/pv_climate_controller`, and open
`docs/v2.1_simulation.html` in an offline browser. The Lovelace YAML is a
draft only. Do not deploy it from this repository; do not modify legacy
automations. Existing thermal, power, house-learning, and manual-takeover
records are preserved across upgrade/restart.
