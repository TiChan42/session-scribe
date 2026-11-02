# Broker und Worker-Handoffs

Der Broker-Kern ist implementiert und lokal mit synthetischen Workern geprüft.
Er führt selbst keine Hostbefehle aus. Die native Supervision mit festen Units,
Benutzern, Mount-Rechten und überprüfter Cgroup-Beendigung ist noch anzubinden.
Die Demo verwendet eine synchrone Fixture für diesen Supervisor; ihr Erfolg ist
kein Container-, Rechte- oder Produktionsnachweis.

## Zuständigkeiten

`Broker` verwendet genau einen Mandanten-Store und ein `Workspace` pro Phase.
Jede Phase benötigt drei getrennte Wurzeln: `inbox`, `outbox`, `cache`. Diese
müssen untereinander und zum Recorder-Store disjunkt sein. Persistente Identitäts-
Marker verhindern versehentliche Wiederverwendung als anderer Mandant oder
andere Phase. Die Konfiguration kommt ausschließlich vom Operator; kein Job
liefert Dateiwurzeln, URLs, Units, Images oder Shellbefehle.

Ein vertrauenswürdiger Runner im separaten Infrastrukturprojekt muss diese
Schnittstelle erfüllen:

| Methode | Erforderlicher Nachweis |
| --- | --- |
| recover(phase) | Verwaiste feste Worker beenden/abgleichen; bei STT alle eingerichteten Guilds global erfassen |
| run(prepared) | Ausschließlich festgelegte Unit/Image starten, begrenztes Timeout, keine Inhalte protokollieren |
| quiesce(prepared) | Ende der gesamten Worker-Cgroup bestätigen, einschließlich Conmon-/Hilfsprozessen |

Ein erfolgreicher Clientprozess oder `systemctl stop` allein ist kein Nachweis
leerer Cgroups. Bei false oder Fehler aus `quiesce` bleibt der Job running und
wird nicht als erledigt markiert. Ein weiterer Aufruf darf ohne erfolgreiche
`recover`-Bestätigung keinen Worker starten. Der globale STT-Lock wird über
Recovery, Übernahme, Lauf, Beendigung und Ergebnisprüfung gehalten.

## Eingaben pro Versuch

Ein Versuch erhält einen eigenen Namen `<job_id>-<lease_token>`; beide Teile
sind validierte UUID-Hexwerte. Die Jobidentität wird vor und nach der Vorbereitung
gegen die aktuelle SQLite-Lease geprüft. Ein Bindungsobjekt enthält Mandant,
Phase, Job-ID, Token und SHA256 des kanonischen Eingabesnapshots. Diese Werte
sind lokale Zuordnungsdaten, keine Signatur oder Netzwerk-Authentifizierung.

Vor der Audioübernahme werden Session, Teilnehmer, Zeitgrenzen, Anzahl und
Audiofenster validiert. Dateien werden mit begrenztem Puffer kopiert, ohne
Symlinks, Spezialdateien oder Hardlinks zu übernehmen. Größe, FLAC-Kennung,
SHA256 und Änderung während des Kopierens werden geprüft. Pro Versuch werden
nur die konkret benötigten Audios kopiert; Recorder-DB und Secrets fehlen.
Snapshot und Modellrevision bleiben über Wiederholungen identisch.

Der Broker versiegelt den neu angelegten Eingabebaum mit Dateimodus 0400 und
Verzeichnismodus 0500. Das ist nur eine lokale Eigentümerprüfung. Die spätere
Infrastruktur muss gezielte Gruppen-/User-Namespace-Zuordnung und Read-only-
Mounts für den jeweiligen Stage-Benutzer herstellen und praktisch prüfen.
Keine pauschalen Leserechte oder gemeinsamen beschreibbaren Mandantenbäume.
Worker schreiben ausschließlich in den separaten Ausgabebaum.

Für die Auswertung wird ausschließlich das bereits committete STT-Transkript
mit einem Session-Snapshot und dessen Hash übergeben. Der Recorder-Store wird
nicht in den Auswerter gemountet. Der Auswerter hält eigenes Budget und Cache
unter `/state` und verweigert nachträglich veränderte Session-/Transkript-Snapshots.
Cloud bleibt ohne Konfigurationsfreigabe und Zustimmung aller relevanten Sprecher
gesperrt; ein deaktivierter Cloud-Broker übernimmt keine neuen Jobs.

## Ausgaben und Wiederanlauf

Worker schreiben zuletzt ein `complete.json`. Es bindet Session, Versuch und
bekannte Ausgabedateinamen an Hashes. Der Broker akzeptiert ausschließlich die
vorbereiteten Bindungsdaten. STT muss die gepinnte Modellrevision und int8 melden.
Alle Chunks benötigen einen gültigen Checkpoint, einschließlich leerer Ergebnisse
für Stille; deren zusammengesetzte Segmente müssen exakt dem Endtranskript
entsprechen. Sprecher, Intervalle, Segment-IDs und Lücken werden geprüft.

Nach bestätigter Beendigung können gültige Teil-Checkpoints in den privaten
Broker-Cache übernommen werden. Ein neuer Versuch erhält neue Ausgabepfade und
nur validierte Checkpoints desselben Eingabesnapshots. Alte Worker erhalten
keinen Schreibzugriff auf diesen Cache. Ungültige oder fehlende Teile werden
nicht übernommen. Anzahl, Einzeldateigröße und Gesamtgröße sind begrenzt.

Die Auswertung wird erneut gegen Schema und Quellen des Eingabesnapshots geprüft.
Markdown wird deterministisch aus dem JSON abgeleitet; ein vom Worker abweichend
geliefertes Markdown wird abgelehnt. Modell-/Prompt-Einstellungen müssen dem
Session-Snapshot entsprechen. Das beweist keine semantische Wahrheit.

Ergebnisdateien werden zunächst unter `results/<job_id>-<token>/` im Recorder-
Store geschrieben. Erst danach committet eine SQLite-Transaktion Ergebnisbeleg,
Jobabschluss und gegebenenfalls den Folgeauftrag zur Auswertung. Der Cloud-
Folgeauftrag entsteht nur bei aktivierter und vollständig zugestimmter Verarbeitung.
Bei DB-Fehler oder veralteter Lease gibt es keinen gültigen Ergebniszeiger.

Leser verwenden `Store.artifact()` und prüfen den committeten Beleg samt Hash.
Ein Crash vor dem DB-Commit kann verwaiste Dateien hinterlassen; sie werden nicht
als abgeschlossenes Ergebnis gelesen. Die lokale Eigentümerlöschung entfernt
auch solche Versuchsdateien für die zugehörigen Jobs. Broker-/Evaluator-Caches
und Backups benötigen zusätzlich den übergreifenden Infrastruktur-Löschauftrag.
Fehlgeschlagene Audios und Snapshots werden nicht automatisch gelöscht.

Kopien mehrerer Versuche zählen gegen das Stage-Diskbudget. Admission und harte
Quoten müssen diesen Platz einschließlich Checkpoints und Reserve berücksichtigen.
Bei erschöpfter Reserve bleibt die Queue stehen; es gibt keinen automatischen
Lösch- oder Quotenvergrößerungsmechanismus.

## SQLite-Migration

Schema 5 verwendet numerische Guild-IDs und einen getrennten Modus; es enthält
`job_results` mit Foreign Key auf `jobs` und Löschkaskade sowie Analysepläne und
archivierte fehlgeschlagene Jobs.
Die Initialisierung/Migration konformer Altversionen auf 5 läuft in einer SQLite-Transaktion;
vorhandene Sitzungen, Jobs, Budgets und Zustellungsbelege bleiben erhalten.
Unbekannte Schema-Versionen werden abgelehnt. Vor einem späteren produktiven
Versionswechsel konsistentes Backup anlegen. Kein automatisches Downgrade;
ältere Images benötigen gegebenenfalls ihren passenden vollständigen Backupstand.
Vor einer Migration legt der Store eine private konsistente Kopie unter
`migration-backups/schema-<version>-before-5-<id>.sqlite3` an. Eine neue Kopie je
Migrationsversuch verhindert das Überschreiben eines früheren Wiederherstellungspunkts.
Mandantenprüfung und Platzreserve gehen der Migration voraus.

Stores früherer Entwicklungsstände mit `meeting`/`dnd` oder dem damaligen
Modusnamen als Tenant und ohne separate Modus-Metadaten werden abgelehnt. Ohne
explizite Guild-Zuordnung darf keine automatische Umhängung oder Umdeutung
vorhandener Daten stattfinden.

Nach dem LLM-Commit wird bei konfiguriertem Ziel atomar die Protokoll-Outbox
angelegt. Sie ist von der Aufnahme getrennt. STT-Aufträge entstehen nach Stop
automatisch für Sitzungen mit Audio; manuelles process ist idempotent. Schema 4
ergänzt Outbox und eindeutige aktive Voice-/Bot-Belegung; bestehende Sessions
ohne Protokollziel werden nicht rückwirkend veröffentlicht.

Ein neuer Analyseplan ist ein eigener logischer Auftrag, kein Zurücksetzen eines
Retry-Zählers. Nur ein toter Analysejob ohne Ergebnis- oder Versandbeleg darf
ersetzt werden. Archivierung, neue Job-ID und Plan werden gemeinsam committed.
Der Plan bindet den unveränderten Aufnahme-Snapshot per SHA256 und speichert die
gewählten Inferenzlimits und optionale Denkstufe. Ändert sich die Quelle oder der
Plan während der Verarbeitung, wird kein Ergebnis committed. Evaluator-Caches
sind je Guild und Job getrennt; das Gateway begrenzt alle Aufträge derselben Guild
weiter gemeinsam. Löschen berücksichtigt auch archivierte Job-IDs und Ergebnisse.

## Nativer Adapter und Inferenztransport

Im separaten Infrastrukturprojekt liegen jetzt ein nativer Runner und ein eigener
Inferenzgateway. Der Runner verwendet feste Stage-Benutzer, unveränderliche Images,
kontrollierte Mounts und systemd-Units. Jeder Stage-Lock umfasst Recovery, Ausführung
und Beendigungsnachweis über alle Guilds. Auch Auswertung ist global seriell;
sie verwendet einen eigenen Lock, unabhängig vom STT-Lock.

Der Adapter beendet den gesamten dedizierten Benutzer-Manager. Er prüft dessen
inaktiven Zustand, rekursives `cgroup.events` und verbliebene Prozesse dieser UID,
einschließlich CLI-Helfern außerhalb der User-Cgroup. Zeitüberschreitung beendet
auch die Prozessgruppe des Starter-/Warteprozesses. Unklare Beendigung blockiert
weiterhin die Lease-Freigabe. Diese Logik ist synthetisch geprüft, noch nicht
mit tatsächlich provisionierten Benutzern/Containern auf dem Zielhost.

Für STT bleiben mindestens 3584 MiB freier RAM und 2 GiB freie Workerablage nötig.
Der kleine Auswerter verlangt separat 768 MiB verfügbaren RAM und 128 MiB freie
Ablage sowie begrenzte Last. Dadurch blockiert das STT-Diskbudget nicht pauschal
den kleineren Auswertungsbereich. Harte Container-/Datenträgerlimits gelten zusätzlich.

Der Auswerter kann ohne IP-Netz an einen explizit eingebundenen Unix-Socket senden.
`unix:///gateway/inference.sock` ist die einzige Unix-Origin im Worker-Vertrag;
alternative Unix-Ziele und HTTP werden abgewiesen. HTTPS bleibt für getrennt
provisionierte Gateways unterstützt. Der Hostgateway akzeptiert ausschließlich
`POST /api/chat` am vorhandenen Ollama-Loopbackdienst. Er prüft Peer-UID, befristete
root-ausgestellte Job-Berechtigung, Hash der exakten Anfrage, freigegebenes Modell,
Kontext-/Ausgabelimits und persistente Tagesbudgets je Guild plus Gesamtbudget.
Ein Credential gilt für genau einen Versuch, auch wenn die Cloud danach ausfällt.
Keine Tools, Admin-Endpunkte, Proxies, Redirects oder stilles Abschneiden.

Host-Broker, fester Supervisor, Rechteübergaben und Ausstellung/Widerruf der
Job-Credentials sind inzwischen verbunden und synthetisch geprüft. Offen bleiben
Ansible-Apply, tatsächliche Linux-Peer-UID-/Isolationsprüfungen und
eine ausdrücklich freigegebene synthetische Cloudprobe.
Vorbereitetes Quellmaterial allein ist kein betriebsbereiter Hostdienst.

Die native Ansible-Rolle bereitet Units und enge Host-ACLs auf vier bereits
provisionierten Bereichen vor. Der Gateway besitzt eine eigene UID und private
Quota-Datenbank; der Auswertungsworker kann diese nicht zurücksetzen. Aktivierung
verlangt einen aktuellen Linux-Prüfbericht, gebunden an App-Revision, native Quellen
einschließlich Unit-/Rechtevorlagen, Image-Digests und UIDs. Abschalten prüft zusätzlich
verbliebene Worker-UIDs außerhalb der Cgroups. Ein eigener Katalog beschränkt
Inferenz auf das für diese Konfiguration qualifizierte Modell und dessen Grenzen.
Quellcode und synthetische Parsertests stellen diesen Betriebsnachweis nicht aus.

Der Host-Broker läuft als eigener unprivilegierter Hostprozess unter der Control-
UID. Er öffnet Recorder-SQLite einschließlich WAL ausschließlich mit dieser UID.
Sein enger Supervisor-Socket wird in keinen Anwendungscontainer eingebunden.
Das Protokoll erlaubt nur Phase und gebundene Job-IDs; keine Images, Benutzer,
Dateipfade, Shellbefehle oder beliebigen Units. Der Root-Supervisor öffnet keine
Recorder-Datenbank. Er prüft Peer-UID, Handoff-Binding und Konfigurationshash,
startet nur feste Worker und quittiert nach vollständiger Beendigung und Rückgabe
der Dateirechte. Ein abgebrochener Caller löst einen lokalen Notstopp aus.

Analyse-Handoffs enthalten nun `worker-config.json`; die gesamte Konfiguration ist
über einen kanonischen Hash gebunden. Set-/Rollen-IDs werden vor der Hashbildung
sortiert, unabhängig vom Python-Hashseed. Die maximale Worker-Laufzeit erhält eine
4500-Sekunden-Lease einschließlich Snapshot-/Start-/Stopreserve. Ein abgelaufener
Lease darf auch nach sicherem Worker-Ende kein Ergebnis committen. Gültige STT-
Checkpoints können nach Recovery erneut genutzt werden; ein vollständig fertiger,
noch live gebundener Versuch wird ohne neuen Worker übernommen.

Der Client pollt ausschließlich committierte Protokolle. Eine belegte Broker-
Sessionsperre lässt die Outbox pending; ein Konflikt nach einem möglicherweise
abgesendeten Request bleibt uncertain. Discord-Mentions und Link-Embeds sind
unterdrückt. Neustart und unklare Quittungen lösen keinen automatischen Doppelpost aus.
