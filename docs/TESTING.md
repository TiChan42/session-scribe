# Prüfung und offene Nachweise

Aktuell bestehen 171 fokussierte Anwendungstests, einschließlich Discord-Steuerungsclient,
Broker-, Ergebnisbeleg- und Migrationstests. Dazu kommen vier separat ausgeführte
Host-Core-Tests, 46 synthetische Native-/Integrationstests und vier Registry-Tests
im separaten Infrastrukturprojekt. Ansible-Syntax/Lint sowie unverändertes lokales
Rendern der tatsächlichen nativen Vorlagen mit `changed=0` sind geprüft;
das ist keine Idempotenz- oder Isolationsprüfung auf dem Server.

Die reguläre Suite läuft mit Python 3.12 und `requirements/dev.txt`. Der schlanke
Host-Core ist zusätzlich mit Python 3.14 und `requirements/host.txt` geprüft:

```sh
PYTHONPATH=src python3.14 -W error::ResourceWarning -m pytest -q host_tests
```

Hierfür eine getrennte Umgebung mit dem Host-Lockfile verwenden. Es enthält
Prüfwerkzeuge; der spätere produktive Broker benötigt nur `requirements/core.txt`.
Audio-/Discord-/STT-Extras bleiben auf Python 3.12. Host-Core-Tests prüfen Import-
trennung, SQLite-/Queue-/Backup-Verhalten, Guild-Persistenz und Ressourcen-Admission.
Der eigene Core-CI-Job installiert keine Audio-/Modellbibliotheken.

Infrastrukturtests prüfen feste Worker-Argv, disjunkte Mounts, Timeout und verwaiste
Helfer, verweigerte fremde Identitäten und ausbleibende Retry-Freigabe. Gatewaytests
prüfen befristete Peer-/Job-Grants, exakte Requesthashes, Einmalverwendung, parallele
Budgetreservierung, Guild-/Gesamtkontingente, Neustartpersistenz und gesperrte Tools/
Routen/Redirects. Unix-Clienttests verwenden ausschließlich einen kurzlebigen lokalen
synthetischen Socket. Kein Test dieser Gruppen startet einen echten Container oder
Ollama; Linux-Peer-UID-, Container- und Host-Isolationsnachweise bleiben offen.

Alle automatischen Proben verwenden synthetische IDs, Texte und Audios. Keine
Tests benötigen einen Discord-Token, echte Gespräche oder einen Cloud-Account.
Produktionsinhalte gehören weder in Fixtures noch in CI-Artefakte.

```sh
.venv/bin/ruff check src scripts tests host_tests
.venv/bin/ruff format --check src scripts tests host_tests
.venv/bin/pytest -q
.venv/bin/python scripts/check_public.py
.venv/bin/python -m build --wheel --no-isolation
.venv/bin/python scripts/check_public.py --wheel dist/*.whl
```

Geprüfte Bereiche: numerische ACLs/Eigentümer, Zustimmung und Widerruf, Beitritt
und Rejoin, sichtbarer Aufnahmestatus, Notstopp ohne Netzwerk, FLAC-Roundtrip,
Segmentgrenzen, gemeinsame Sprecherzeitachse mit Überlappung/Stille/Lücken,
RTP-Wrap, Duplikate, Quota-/Dauer-/Speichermangel und offene Crash-Journale.
Dazu Queue-Übernahme/Leases/Backoff, globaler Lock über Prozessgrenzen,
Ressourcen-Admission, persistente Checkpoints, Audiohashes, getrennte Evaluator-
Handoffs, Quellen-/Charakterprüfung, invalide JSON-/Tool-Antworten, Kontextüberlauf,
Budget bei Cloud-Ausfall, Prompt-Injection als Daten, Mention-Schutz und unklare
Publikationsquittungen. SQLite-Backup wird synthetisch wiederhergestellt.

`check_public.py` prüft öffentliche/staged Pfade, Private-Key-Header, Wheelinhalt
und die deny-by-default Container-Kontextliste. Das ist kein universeller Token-
Scanner. Zusätzlich vor Veröffentlichung staged Diff, konkrete private Werte,
alle COPY-Eingaben und Artefakte prüfen. `.gitignore` schützt keine bereits
getrackten oder erzwungen hinzugefügten Dateien. Lokal wurde nichts gestaged.

## Synthetischer vertikaler Weg

```sh
.venv/bin/sessionscribe demo --root .cache/demo-meeting --mode meeting
.venv/bin/sessionscribe demo --root .cache/demo-dnd --mode dnd
```

Jeweils neues Verzeichnis verwenden. Echtes FLAC, echte Queue und echte
Validierung; STT-Text und Cloud-Antwort sind Fixtures. Die Demo ist kein Beleg
für Spracherkennung, Internetzugang oder Discord-Aufnahme.

## Echte lokale CPU/int8-STT mit künstlicher deutscher Sprache

Nur Operator-Vorbereitung lädt das Modell. Das gepinnte Modell benötigt rund
0,5 GB Download; Worker laden weder im Netz nach noch andere Modellfamilien.
STT-Abhängigkeiten separat installieren:

```sh
.venv/bin/pip install --require-hashes -r requirements/stt.txt
.venv/bin/python scripts/fetch_model.py .cache/model-small
# Auf macOS, nur synthetische TTS-Datei; keine Mikrofonaufnahme:
say -v Anna -o .cache/synthetic-de.aiff 'Wir haben keinen Beschluss. Ada prüft zwölf Geräte bis Freitag.'
ffmpeg -nostdin -hide_banner -loglevel error -i .cache/synthetic-de.aiff -ar 48000 -ac 2 -c:a flac .cache/synthetic-de.flac
.venv/bin/python scripts/smoke_stt.py --root .cache/real-stt-smoke --model .cache/model-small --synthetic-audio .cache/synthetic-de.flac
```

Das Testskript verlangt einen neuen Zielordner. Die künstliche Sprache wird
über den Recorder in einen Handoff geschrieben; ein separater Python-Prozess
führt den echten Offline-Worker aus. Start, Zustimmung und lokaler Stopp laufen
über die Controller-Policy mit synthetischer Statusquittung. Die Engine-Probe
verwendet einen explizit ungebundenen lokalen Handoff und keinen Host-Runner.
Kein produktiver Zustand wird dafür verwendet.

Ausgeführt am 2026-11-01 auf einem lokalen ARM-Mac: faster-whisper 1.2.1,
CTranslate2 4.6.0, multilingual small, CPU/int8, zwei Threads. Ergebnis:
„Wir haben keinen Beschluss, Ada prüft zwölf Geräte bis Freitag.“ Laufzeit
8,6 s, gemessener Peak-RSS 923123712 Bytes. Einzelne synthetische Kurzprobe,
keine Aussage über reale Meetings, Akzente, Nebengeräusche oder Langzeitlast.
Prozessmessung beweist keine Container-RAM-/CPU-Grenze oder Zielhostperformance.
Die TTS-/ffmpeg-Werkzeuge sind nur lokale Fixture-Vorbereitung, keine Runtime-
Abhängigkeiten des Recorders; alternative explizit synthetische FLACs sind möglich.

## Deploymentvorbereitung

Im separaten Infrastrukturprojekt wurden Syntaxcheck und ansible-lint für den
neuen lokalen Renderer ausgeführt. Erster Vorbereitungslauf änderte drei lokale
Datei-/Verzeichniszustände; der zweite unveränderte Lauf meldete `changed=0`.
Das belegt ausschließlich lokale Vorbereitung, keine angewandte Hostkonfiguration.

Python-Wheelbau und Hash-Abhängigkeitsauflösung für Linux x86_64/Python 3.12
wurden lokal geprüft. Docker/Podman sind hier nicht verfügbar; OCI-Builds und
Runtime-Isolationstests liefen nicht. GitHub-Workflows sind vorbereitet, noch
nicht remote ausgeführt. Release-Tests verweigern Root-Images, falsche Revisionen,
eingebettete Credential-Variablen, zusätzliche Archivdaten und veränderte Blob-Hashes.
Die Auslieferung verlangt gesondert die tatsächlich eingerichteten Branchregeln.

## Ausstehende Abnahme vor Produktion

1. Gepinnter lizenzgeprüfter DAVE-Empfänger, echte autorisierte Mehrsprecheraufnahme
   mindestens 30–60 Minuten, inklusive Beitritt, Rejoin, Pause/Stop und längerer Stille.
   Entschlüsseltes Audio aller Sprecher abhören, zeitliche Referenzereignisse prüfen;
   Join oder Playback genügen nicht. Keine Aufnahme ohne eigene Freigabe.
2. Native Supervision an den lokal geprüften Broker anbinden und Ganz-Cgroup-Timeout
   praktisch integrieren; global
   ein STT unter parallelen Meeting-/DnD-Aufträgen und Ressourcenmangel beweisen.
3. OCI-Builds, AppArmor/Seccomp/NNP/Non-Root, Quoten, tatsächliches ENOSPC und
   blockierte Cross-Tenant-/Host-/Metadatenzugriffe einschließlich IPv6 prüfen.
   Die lokalen Speichermangeltests simulieren Admission, kein echtes volles Host-FS.
4. Eigene Voice-UDP-Policy einschließlich IPv6 erlaubte Ziele/Antworten, Zielwechsel,
   Ablauf und verbotene Ziele praktisch testen.
5. Eigener Inferenzzugang, reale Anbieterlimits/Tokenizer und deutsche Ergebnisqualität
   mit freigegebenen synthetischen Proben; kein Cloud-Call wurde bisher ausgeführt.
6. Vollständiger Offsite-Restore und revisionsbezogene Readiness/Deploy-Rollback,
   separater Forced-Command-Key, Branchschutz und zweiter unveränderter Host-Apply.
7. Registrierte Slash Commands und begrenzte große Ergebnisanhänge; Notstopp und
   sichtbare Statusführung auf Discord tatsächlich testen.

## Zusätzliche Broker-Prüfung

Die aktualisierte Meeting-/DnD-Demo verarbeitet beide Queue-Phasen über den Broker
und einen synchronen Fixture-Runner. Geprüft sind neue Versuchs-Token, versiegelte
Input-Bäume, fehlende/falsche Hashes und Quellenintervalle, falscher Mandant/Job,
abgelaufene Lease, unklare Worker-Beendigung, globale Sperre vor Übernahme,
partieller Timeout mit Checkpoint-Wiederverwendung und Änderungen am Eingabesnapshot.
Weitere Tests prüfen DB-Rollback bei Folgeauftragfehler, manipulierte Ergebnisbelege,
Löschung committierter und verwaister Dateien sowie Migration von Schema 1 auf 4.

Nach der Handoff-Änderung wurde die echte synthetische deutsche STT erneut
ausgeführt: 7,16 s, Peak-RSS 982007808 Bytes auf demselben lokalen Mac. Der Text
erhielt weiterhin Verneinung, Namen, Zahl und Frist der künstlichen Probe.
Native Container-, Cgroup-, Dateirechte- und Netzwerkisolation sind damit weiterhin
nicht geprüft. Es gab keinen Cloud-Aufruf oder Live-Discord-Aufnahmetest.

Die neue Guild-Konfiguration ist mit einem gemeinsamen Bot-Konfigurationsregister
für mehrere Guilds geprüft: Meeting und DnD als getrennte Modi, persistente
Konfiguration, Manage-Guild-Prüfung, fremde Kanal-/Rollenverweigerung, lokale
Moduswechsel, getrennte Sitzungen/Budgets auch bei gleichem Modus und blockierte
Änderung während aktiver Aufnahmen. Diese Policy-Tests registrieren keine Discord-Commands.

## Discord-Steuerungsclient

Synthetische Tests prüfen reale Pycord-Commandschemas inklusive Unterbefehltypen,
authentische Interaktionsidentität, Manage-Guild-Prüfung, Guild-Kapazität/Allowlist,
dauerhaftes Setup, fremde/partielle Kanäle, Anhänge-/Mention-Rechte, No-Follow-
Credential-Lader, geschlossene Voice-/Cloud-Gates und begrenzte Interaktionen.
Readiness verlangt erfolgreiche Registrierung, frische Zeit und richtige Revision;
Fehlermeldungen enthalten keine Exception-Inhalte. Abbruch schließt Client/SQLite
und setzt Bereitschaft zurück. Die Tests senden keine Netzwerkrequests.

Zusätzliche manuelle Probe: Der Steuerungsclient hat am 2026-11-01 eine echte
Discord-Gateway-Verbindung hergestellt, alle 13 globalen Commands registriert
und über die API rückgeprüft. Frische Control-Bereitschaft und sauberer Abbruch
wurden bestätigt. Keine Kanalnachricht, Aufnahme oder Cloud-Anfrage. Das war
ein lokaler kurzlebiger Client, kein Serverdeployment und kein Test von
Interaktionen oder Kanalrechten in einer echten Testguild. Private Prüfbelege
bleiben ausschließlich in `.local/`.

## Gleichzeitige Sessions und Protokollrouting

Neue synthetische Tests betreiben zwei Kanäle derselben Guild mit verschiedenen
Aufnahme-Bots, wiederverwenden einen Bot in unterschiedlichen Guilds und prüfen
Bot-/Kanal-/Kapazitätsgrenzen, getrennte Pause/Stop, kontrollierten Shutdown sowie
Frames mit falscher Guild/Bot-/Session-ID. Ein weiterer Test verarbeitet eine
abgeschlossene Sitzung bis zur Veröffentlichungs-Fixture, während eine andere
Sitzung weiter Audio auf Platte schreibt. Kein reales Discord-Audio ist beteiligt.

Routingtests prüfen Default, Override, Vererbung, explizite Deaktivierung, fremde
Guild-Ziele und Persistenz. Versandtests prüfen fehlgeschlagene LLM-Jobs ohne Post,
belegtes LLM-Ergebnis vor Veröffentlichung, Markdown-Anhänge, Kanaländerung und
unklare Quittungen ohne Duplikat. Der Recorder-Test verweigert vollständiges
Audio-Einlesen beim Abschließen; kleine Codec-/Frame-Puffer und native RSS-Grenzen
sind damit nicht vollständig vermessen.

Die zusätzlichen Host-Integrationstests simulieren Linux-UIDs/Root-Verzeichnis-
metadaten ausdrücklich. Sie prüfen Protokoll-Autorisierung, verbotene Hostargumente,
aktives/busy Recovery, passenden Stop, Besitzrückgabe nach Beendigung, sehr frühen
Caller-Disconnect, fairen Wechsel zwischen STT/Analyse und die Übernahme eines
fertigen, noch geleasten Versuchs ohne neuen Worker. Sie beweisen keine echten
Root-Dateirechte oder Linux-Cgroups. Zusätzlich stimmen die Hashes einer identischen
synthetischen Inferenzanfrage zwischen Python 3.12 und 3.14 überein.
