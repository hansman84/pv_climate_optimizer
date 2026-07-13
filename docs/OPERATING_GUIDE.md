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
