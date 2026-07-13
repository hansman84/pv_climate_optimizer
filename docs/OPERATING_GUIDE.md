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
4. Priorität 1–100 setzen; höher bedeutet bei gleicher Temperaturdringlichkeit
   zuerst im Shadow-Plan.

Der bestehende Wohnzimmer-Pilot wird beim ersten Hinzufügen als Hauszone
übernommen. Komforttemperatur und harte Grenze bleiben pro bestehender
Integration steuerbar; neue Zonen verwenden 23,5 °C und 25,5 °C als Standard.

## Betriebsarten

`cool` und `heat` sind temperaturrelevante Betriebsarten. `fan_only`, `dry`,
`auto`, Lüfterstufe und Swing werden sichtbar beobachtet, aber nicht verändert.
So bleiben Lautstärke, Luftführung und Entfeuchtung unter manueller Kontrolle.

## Dashboard

Das Dashboard **PV Klimaregler** zeigt PV-Lage, Überschuss, die aktuelle
Shadow-Entscheidung und verstellbare Sicherheitswerte. Der Haus-Kühlplan
enthält die aktive Zonenzahl, die Zahl thermischer Anforderungen, das
Nennbudget und die aktuelle Prioritätsreihenfolge.

## Produktiver Pilot

Ein produktiver Pilot ist ausdrücklich nicht enthalten. Vor einer späteren
Freigabe müssen Shadow-Plan, Betriebszustände und Hausbudget über reale
Szenarien geprüft und separat abgenommen werden.
