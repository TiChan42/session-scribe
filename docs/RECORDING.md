# Parallele Aufnahme, Queue und Protokollkanäle

Ein gemeinsamer Steuerungsbot verwaltet Guild-Konfiguration und Commands. Ein
operatorverwalteter Pool zusätzlicher Aufnahme-Bots erlaubt gleichzeitig belegte
Sprachkanäle derselben Guild. Pro Bot und Guild wird maximal ein Sprachkanal
reserviert; dieselbe Aufnahme-Identität kann in verschiedenen Guilds gleichzeitig
arbeiten. Der Operator muss Bot-Identitäten autorisieren und die Bots in die
jeweiligen Guilds einladen. Tokens werden niemals über Commands übermittelt.
Die Discord-Beschränkung ist im [Voice-Guide der SDK-Maintainer](https://github.com/discordjs/guide/blob/main/guide/voice/voice-connections.md)
beschrieben. Es wird kein Wechsel aus einem bereits aufgenommenen Kanal erzwungen.

## Alias und Sitzungszustimmung

`/alias` ist an den Befehlschat und den Sprachkanal gebunden, in dem die Person
gerade ist. Der Benutzer gibt seinen Namen im Protokoll ein und wählt im
Pflichtfeld, ob die konkrete Sitzung aufgezeichnet werden darf. Ohne neuen
Namen wird der eigene, für diesen Sprachkanal gespeicherte Alias übernommen.
Profile sind pro Guild/Sprachkanal gespeichert; sie enthalten keine Aufnahme-
oder Cloudzustimmung. `/alias-delete` löscht das eigene Profil und widerruft
eine aktive Sitzungszustimmung. Bereits abgeschlossene Session-Snapshots,
Backups oder veröffentlichte Protokolle werden dadurch nicht nachträglich geändert.

Eine Session bleibt wartend, bis jeder aktuelle Teilnehmer einen eindeutigen
Alias hat und `/alias` für diese Session akzeptiert hat. Vor Resume wird die
aktuelle Voice-Belegung aus Discord eingelesen. Ein Join pausiert die Aufnahme
vor weiteren akzeptierten Frames; ein gespeicherter Alias kann die Anzeige
füllen, nie aber die Zustimmung. Der Befehlschat meldet den Join sofort und
erinnert bei ausstehender Zustimmung frühestens nach 60 Sekunden, anschließend
minütlich. `/pause` und `/pausesession` schließen die aktuelle Datei; `/resume` startet erst nach
erneuter Vollprüfung. Weder Zustimmung noch Verbindung lösen automatischen
Wiederanlauf aus. Der Live-Empfang bleibt bis zum geprüften DAVE-Test gesperrt.

## Protokollziele

| Einstellung | Verhalten |
| --- | --- |
| Guild-Default gesetzt, kein Sprachkanal-Override | Protokoll geht in den Default-Kanal |
| Sprachkanal hat eigenes Ziel | Dieses Ziel hat Vorrang |
| Sprachkanal ist explizit deaktiviert (`null`) | Keine automatische Veröffentlichung, auch mit Default |
| Override entfernt / Vererbung gewählt | Guild-Default wird wieder verwendet |
| Weder Default noch eigenes Ziel | Ergebnis bleibt lokal abrufbar |

Vorbereitete Handler/Slash-Definitionen:

```text
/config default_protocol channel:#protokolle
/config protocol voice_channel:Besprechung channel:#meeting-protokolle
/config protocol voice_channel:Besprechung inherit:true
/config protocol voice_channel:Besprechung
```

Ohne `channel` und ohne `inherit` deaktiviert der letzte Befehl die Veröffentlichung
für diesen Sprachkanal. Der Default-Befehl ohne Kanal entfernt den Guild-Default.
Neue `/setup`-Einrichtungen verwenden ihren ausdrücklich gewählten Output-Kanal
auch als Default-Protokollkanal. Bestehende Konfigurationen ohne Protokollziel
werden nicht rückwirkend aktiviert. Statusmeldungen bleiben davon unabhängig sichtbar.
Alle Zielkanäle müssen verifizierte Textkanäle derselben Guild sein. Änderungen
benötigen Manage Guild und werden während aktiver/ausstehender Verarbeitung gesperrt.
Die Commands sind noch nicht live registriert.

Beim Start werden Bot-ID, Statuskanal und aufgelöstes Protokollziel in der Session
festgehalten. Ein späterer Konfigurationswechsel darf alte Ergebnisse nicht still
umleiten. Der Publisher prüft deshalb zusätzlich die aktuelle Freigabe. Bei einer
Abweichung bleibt die Veröffentlichung gesperrt; Original und Ergebnis bleiben lokal.

## Audio auf Platte

Der Recorder erhält pro Sprecher genau 20 ms PCM16 Stereo bei 48 kHz: 3840 Bytes
pro Frame. Er schreibt direkt in kurze verlustfreie FLAC-Dateien. Kleine Frame-,
Codec- und Betriebssystem-Puffer benötigen RAM; die Gesamtsitzung wird dort nicht
gesammelt. Fertige Segmente werden mit 64-KiB-Lesepuffer gehasht. Im Recorder
bleiben nur laufende Dateischreiber und Zähler, keine Liste aller Audioblöcke oder
fertigen Segmentmetadaten. Whisper/CTranslate2/Torch werden dort nicht importiert.

`RecordingPool` reserviert einen freien `(bot_id, guild_id)`-Slot und begrenzt
aktive Sessions, maximale Spuren sowie konservative Audioreserve global.
Ausgangslimits: höchstens vier Sitzungen, 32 reservierte Spuren und 4 GiB
Audioreserve, plus 512 MiB freie Reserve. Das sind Obergrenzen, keine Zusage für
die aktuelle Hardware. Vor jedem Start zählen noch ungenutzte Reservierungen auf
demselben Dateisystem mit; wartende Audios belegen weiterhin echte Diskkapazität.
Wenn Ressourcen oder Aufnahme-Bots fehlen, wird vor dem Start abgelehnt.

Pause schließt nur die Dateien der betroffenen Sitzung und behält den Slot.
Stop schließt sie, gibt genau diesen Slot frei und reiht die Sitzung automatisch
ein. Weitere Aufnahmen laufen unverändert weiter. Session- und Bot-Belegung sind
zusätzlich durch SQLite-Indizes gegen doppelte aktive Zuordnung geschützt.
Shutdown unterbricht und schließt alle Sitzungen, ohne heimlichen Wiederanlauf.
Unfertige Teile bleiben bei Fehlern zur Prüfung erhalten.

Der Pool und seine eigenen SQLite-Verbindungen gehören zum Recorder-I/O-Loop.
Discord-/Hilfsprozesse dürfen nur begrenzte Frame-Übergaben verwenden; bei Überlast
pausieren/stoppen statt eine unbeschränkte RAM-Queue aufzubauen. Native Aufnahme-
Adapter, Tokenladung und sichere IPC-/Prozesszuordnung sind noch nicht integriert.

## Verarbeitung und Versand

Jede geschlossene Sitzung mit Audio erhält einen dauerhaften STT-Auftrag. Leere
Starts ohne Audios werden nicht als Spracherkennungsjob eingereiht. Die Aufnahme
wartet nicht auf STT oder Cloud. Der separate Broker verarbeitet Sessions über
Guild-Queues; ein Round-robin-Scheduler verhindert Bevorzugung einer einzelnen
Guild. Global bleibt höchstens ein STT-Worker aktiv. Danach folgt die freigegebene,
budgetierte LLM-Auswertung. Rohtranskript und Audio werden nicht durch das Modell
überschrieben. Schema-/Quellenprüfung beweist weiterhin keine semantische Wahrheit.

Erst der erfolgreiche Commit des validierten LLM-Ergebnisses erzeugt atomar den
Eintrag in `protocol_outbox`. Bei Cloud-Ausfall oder ungültigem Ergebnis wird kein
Protokoll gepostet. `ProtocolPublisher` konsumiert diese Outbox getrennt von der
Aufnahme. Unter 1900 Zeichen verwendet der vorbereitete Transportvertrag Text,
sonst eine Markdown-Datei mit maximal 2 MB Anwendungsgrenze. Keine stillen Kürzungen,
keine Massenmentions, keine frei wählbaren Ziele.

Outbox-Zustände: pending, sending, sent, uncertain, blocked. Nach Crash oder
verlorener Quittung wird ein möglicherweise gesendetes Protokoll nicht automatisch
nochmals gepostet. Der Operator muss den Zielkanal abgleichen. Ein vollständiger
Live-Discord-/DAVE-/Container-Nachweis steht noch aus; lokale Tests verwenden
synthetische Audio-, LLM- und Versand-Fixtures.
