# Produktive Kühlstrategie – Entwurf

## Ziel

Das Haus soll komfortabel bei möglichst hohem PV-Eigenverbrauch gekühlt werden.
Die Steuerung nutzt die vorhandenen Innengeräte als Zonen einer gemeinsamen
Hisense-5-fach-Außenanlage. Sie startet nie aufgrund eines einzelnen Werts,
sondern nur aus einer bestätigten thermischen und energetischen Entscheidung.

Dieser Entwurf ist **nicht aktiviert**. Der bestehende Shadow Mode bleibt die
Vorgabe; eine produktive Ausführung benötigt eine eigene, sichtbare Freigabe.

## Entscheidungsmodell

Pro Auswertung bildet der Regler eine priorisierte Kandidatenliste:

1. **Sicherheitsprüfung:** Temperaturquelle plausibel und frisch,
   Innengerät erreichbar, kein manueller Override, kein beobachteter Heizmodus
   in einer anderen Zone und keine Überschreitung der Außenanlagen-Kapazität.
2. **Thermischer Bedarf:** Bereits über Komforttemperatur erhält ein Zimmer
   Punkte je Grad. Die harte Grenze erhält einen großen Sicherheitsaufschlag.
   Ein positiver Temperaturgradient und eine prognostizierte Überschreitung
   innerhalb von 60 Minuten erhöhen die Priorität frühzeitig.
3. **Komfortpriorität:** Bei vergleichbarer thermischer Lage entscheidet der
   einstellbare Raumwert 1–100.
4. **Energieprüfung:** Die Energiepolitik bewertet aktuellen Export,
   Mindestüberschuss und PV-Prognose. Der Hausplan reserviert Leistung für
   bereits kühlende Innengeräte, bevor er eine weitere Zone vorsieht.

Der Score entscheidet nur die **Reihenfolge**, nicht blind eine Aktion.

## Vorgeschlagene Zustandsmaschine je Raum

```text
BEOBACHTEN
  -> VORBEREITEN        (Prognose: Komfortgrenze in <= 60 min)
  -> KÜHLEN_ANFORDERN   (Komfortgrenze erreicht + Energie/Sicherheit ok)
  -> NOTFALL_KÜHLEN     (harte Grenze erreicht; Komfort hat Vorrang)

KÜHLEN_ANFORDERN / NOTFALL_KÜHLEN
  -> KÜHLT              (Gerät bestätigt cool und Sollwert)
  -> FEHLER_SPERRE      (Bestätigung/Kommunikation fehlgeschlagen)

KÜHLT
  -> NACHLAUF           (Komfortreserve wiederhergestellt und Mindestlaufzeit erreicht)
  -> MANUELL            (Bedienung durch Menschen erkannt)

NACHLAUF -> BEOBACHTEN  (Ausschaltbedingung stabil)
MANUELL  -> BEOBACHTEN  (Override abgelaufen oder ausdrücklich aufgehoben)
```

## Konkrete, konservative Start- und Stoppregeln

| Regel | Vorschlag |
|---|---|
| Start bei Komfortbedarf | Temperatur >= Komfortgrenze **und** mindestens 10 Minuten stabiler Bedarf |
| Vorausschauender Start | Komfortgrenze laut belastbarer Prognose in <= 45 Minuten; nur mit PV-Überschuss |
| Notfallstart | Temperatur >= harte Grenze; Energiepolitik wird überstimmt, Sicherheitsregeln bleiben aktiv |
| PV-Startreserve | Export >= Mindestüberschuss + geschätzte Leistung der neuen Zone; zwei aufeinanderfolgende Messungen |
| Maximale Parallelität | Minimum aus fünf Außengeräteanschlüssen, EMS-Freigabe und freiem Nennbudget |
| Mindestlaufzeit | 20 Minuten ab bestätigtem Kühlbetrieb |
| Stopp bei Ziel erreicht | Temperatur <= Komfortgrenze - 0,3 °C für 10 Minuten und Mindestlaufzeit erfüllt |
| Stopp bei PV-Wegfall | Nach Mindestlaufzeit; in Komfort-/Notfallbetrieb erst bei ausreichender Temperaturreserve |
| Wiedereinschaltsperre | 20 Minuten nach regulärem Stopp |
| Kommunikationsfehler | Ein Wiederholversuch, danach Sperre für 30 Minuten und sichtbarer Alarm |
| Manueller Eingriff | Sofort keine weitere Aktion für dieses Gerät für zwei Stunden |

Die Werte sind absichtlich als Einstellungen vorgesehen, nicht als versteckte
Konstanten. Sie werden zunächst im Shadow Mode gegen Verlaufsdaten kalibriert.

## Klimabefehle und Geräteprofil

Die produktive Route darf keine generischen, geratenen Befehle verwenden. Für
jedes Innengerät wird vor Freigabe ein bestätigtes Profil gespeichert:

- Einschalten: bestätigte Kombination aus `hvac_mode: cool` und, falls nötig,
  dem vom Gerät verlangten Einschaltbefehl.
- Sollwert: `Komforttemperatur - 0,3 °C`, begrenzt auf einen pro Gerät
  konfigurierbaren sicheren Bereich. Ein vorgezogener PV-Start nutzt maximal
  weitere 0,5 °C Absenkung.
- Ausschalten: ausschließlich der bestätigte Aus-Befehl des jeweiligen
  Geräts.
- Nicht automatisch verändert: `dry`, `fan_only`, `auto`, Lüfterstufe,
  Swing, Presets und Heizen.

Jede Aktion wird nach dem Senden am Gerätezustand bestätigt. Ohne Bestätigung
gilt sie als fehlgeschlagen und löst keine Befehlsserie aus.

## Hausweite Aufteilung

Der Regler startet immer nur die bestplatzierte zulässige Zone und bewertet
danach die Hauslage erneut. Dadurch wird die Außenanlage nicht mit mehreren
gleichzeitigen Starts überfahren. Bereits aktive Kühlleistung wird vom
12,5-kW-Nennbudget abgezogen. Ohne glaubwürdige Leistungsquelle einer neuen
Zone wird sie nicht automatisch zusätzlich gestartet; sie bleibt als
„Kapazität unbekannt“ sichtbar.

Eine PV-Prognose wird für vorausschauendes Kühlen nur verwendet, wenn sie eine
zeitliche Aussage liefert. Der heutige Momentanwert darf lediglich ein
aktuelles PV-Fenster bewerten, nicht eine zukünftige Sonne erfinden.

## Bedienung im Dashboard

Die einfache Ansicht erhält nur:

- „Jetzt kühlen: Raum X“ oder „Warten: Begründung“;
- nächstes erwartetes Kühlfenster;
- aktive und wartende Räume;
- PV-Reserve und verbleibendes Außenanlagen-Budget;
- einen eindeutig sichtbaren Automatik-Schalter mit Status `Shadow`,
  `Pilot`, `Automatik gesperrt` oder `Automatik aktiv`.

Die Extended-Ansicht zeigt zusätzlich Rangfolge, Gradient, Prognose, Zeit bis
Grenze, Gerätebestätigung, Mindestlaufzeit, Sperrzeit, Override und jeden
Entscheidungsgrund.

## Freigabeplan

1. **Replay:** mindestens 14 Tage aufgezeichneter Sommerdaten gegen die neue
   Zustandsmaschine auswerten; keine Befehle.
2. **Shadow-Abgleich:** Entscheidungen mindestens sieben Tage live anzeigen
   und fachlich prüfen.
3. **Ein-Zonen-Pilot:** nur Wohnzimmer, nur tagsüber, nur eine bestätigte
   Start-/Stopp-Sequenz pro Tag; jederzeit sichtbarer Not-Aus.
4. **Zwei-Zonen-Pilot:** erst nach fehlerfreier Bestätigung, korrekt erkannten
   Overrides und stabiler Hauskapazität.
5. **Hausfreigabe:** jede weitere Zone einzeln hinzufügen; automatische
   Parallelstarts bleiben verboten, bis reale Leistungsdaten belastbar sind.

Vor Schritt 3 braucht es eine ausdrückliche Freigabe für die exakt getesteten
Climate-Services und Geräteprofile. Bestehende Automationen bleiben dabei
unverändert.
