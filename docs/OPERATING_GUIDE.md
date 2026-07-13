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

## Dashboard

Das Dashboard **PV Klimaregler** ist bewusst in vier Ansichten geteilt:

1. **Übersicht** ist die Alltagsansicht. Sie zeigt die Empfehlung jetzt, die
   hausweite Priorität, PV-Leistung, freien Überschuss und die geschätzte
   Gesamtleistung der gemeinsamen Außenanlage.
2. **Räume** ist die Komfortansicht. Für jeden Raum stehen nebeneinander die
   Temperatur jetzt, die Prognose für 60 Minuten und die aktuelle Priorität.
   `Arbeitszimmer / Spielzimmer` bezeichnet dabei den Raum Arbeitszimmer mit
   dem Klimagerät Spielzimmer.
3. **Steuerung** enthält nur Werte, die sinnvoll und sicher bedienbar sind:
   Shadow Mode, Energiepolitik, Mindestüberschuss und den optionalen
   Temperatur-Backup je Raum.
4. **Technik** enthält Rohwerte, Leistungsquellen und vollständige
   Diagnosen. Sie ist für Fehlersuche und Feintuning gedacht, nicht für den
   täglichen Betrieb.

Raumziele, harte Grenzen und Prioritäten gehören absichtlich nicht zwischen
die täglichen Kacheln. Sie werden sauber pro Zone gesetzt unter
**Einstellungen → Geräte & Dienste → PV Climate Controller → Konfigurieren →
Zonen verwalten**. Die Standardwerte für neu angelegte Zonen sind 23,5 °C
Komforttemperatur und 25,5 °C harte Grenze.

Der **Shadow-Plan** einer Zone enthält Temperatur, Betriebsmodus, Priorität,
BTU/h-Beobachtung und Reason-Code. Der Haus-Kühlplan fasst alle Raumpläne, die
aktive Zonenzahl, thermischen Bedarf, gemeinsames Nennbudget und die aktuelle
Prioritätsreihenfolge zusammen.

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
