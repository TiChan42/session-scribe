# SessionScribe

Für die Inbetriebnahme existiert ein separates, streng begrenztes Einzeltestimage
mit gepinntem DAVE-Empfangskandidaten. Es startet nur mit einer kurzlebigen privaten
Betreiberfreigabe für eine bestimmte Person und bestimmte Kanäle. Der reguläre Bot
behält seine Empfangssperre bis zur vollständigen Qualifikation; Details stehen
in [docs/testing.md](docs/testing.md).

Ein gemeinsamer Steuerungsbot mit Konfiguration je Discord-Server und ein Pool
zusätzlicher Aufnahme-Bots für parallele Sprachkanäle derselben Guild. Die beiden
Modi heißen **Meeting** und **DnD**. Daten, Queues, Ergebnisziele und Inferenzbudgets
sind nach numerischer Guild-ID getrennt, auch wenn mehrere Server denselben Modus nutzen.

Der lokale Kern funktioniert: Zustimmung → begrenzte FLAC-Aufnahme → dauerhafte
Queue → Broker-Handoff → STT → validiertes LLM-Protokoll → Versand-Outbox. Beide Modi laufen in
der synthetischen Demo durch. Echte lokale CPU/int8-STT mit künstlicher deutscher
Sprache ist ebenfalls geprüft; Cloud und Supervisor sind dort Fixtures.

**Entwicklungsstand:** Der Discord-Steuerungsclient und die Registrierung der Slash
Commands sind angebunden; Guild-Konfiguration wird dauerhaft gespeichert. Aufnahme
bleibt bis zum geprüften DAVE-Empfänger gesperrt. Host-Broker, fester Supervisor und eigener
Inferenzgateway sind im Infrastrukturprojekt synthetisch geprüft. Eine native Ansible-Rolle
für Units, Rechte und Gateway-Firewall ist vorbereitet; Provisionierung,
Container-/Linux-Abnahme und Deployment-Apply bleiben offen. Kein produktiver Serverdienst
wurde gestartet.

## Tokens hinterlegen

Das Steuerungsbot-Token in `.local/bot.env` lokal befüllen:

```dotenv
DISCORD_TOKEN=DEIN_BOT_TOKEN
```

Datei 0600, `.local/` 0700. Der Token wird nicht ausgegeben, committed oder ins Image
kopiert. Die Serverbereitstellung ist inzwischen vom Betreiber beauftragt;
der geschützte Übertragungsweg gehört zur noch offenen Deployment-Implementierung.
Der Steuerungsclient liest ausschließlich sein eigenes `DISCORD_TOKEN` aus der Datei.
Zusätzliche Aufnahme-Bots benötigen eigene Tokens in getrennten privaten Dateien
unter `.local/recorders/`; STT und LLM erhalten keine Discord-Tokens. Rechte und
Einrichtung: [Konfiguration](docs/CONFIGURATION.md).

## Lokal prüfen

Python 3.12, keine echten Tokens oder Server nötig:

```sh
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements/dev.txt
.venv/bin/pip install --no-deps --no-build-isolation -e .
.venv/bin/sessionscribe demo --root .cache/demo-meeting --mode meeting
.venv/bin/sessionscribe demo --root .cache/demo-dnd --mode dnd
.venv/bin/pytest -q
.venv/bin/ruff check src scripts tests
.venv/bin/ruff format --check src scripts tests
```

Demo-Zielordner müssen neu sein. Audio ist synthetisch; STT-Text, Cloud und
Worker-Supervision sind Fixtures. Beide Queue-Phasen verwenden den Broker und
committete Ergebnisbelege. Es gibt keine Discord-Verbindung oder Cloud-Anfrage.

## Bedienung und Trennung

Die Konfigurationsbefehle sind `/setup` mit Auswahl **Meeting** oder
**DnD**, `/config show`, `/config mode`, `/config default_protocol` und
`/config protocol`. Ein Sprachkanal erbt das Default-Ziel oder erhält ein eigenes
Protokollziel; automatische Veröffentlichung lässt sich je Kanal deaktivieren. Einrichtung erfordert bei der handelnden
Person die echte Discord-Berechtigung „Server verwalten“. Numerische Rollen-ACLs
und feste Kanäle werden pro Guild gespeichert. Modus/Kanäle lassen sich während
aktiver Aufnahme oder wartender Verarbeitung nicht umstellen. Der Discord-Client
registriert diese sowie die Sitzungsbefehle; `/start` und `/resume` melden das
geschlossene Aufnahmegate. Ein Join oder eine leere Sitzung wird dabei nicht erzeugt.

Für den Steuerungsclient `config/bot.example.json` nach `.local/bot.json` kopieren
und absolute lokale Daten-/Tokenpfade eintragen. Modellname ist bis zur separaten
Cloud-Freigabe nur ein Platzhalter; es erfolgen keine Inferenzanfragen.

```sh
.venv/bin/sessionscribe bot --policy .local/bot.json
.venv/bin/sessionscribe control-health --root /absolute/private/data --revision development
```

Die Healthprüfung verlangt eine frische Quittung derselben Revision und erfolgreiche
Command-Registrierung. Sie bestätigt ausschließlich den Steuerungsclient.

Aufnahmebefehle prüfen Guild, Kanal, Nutzer/Rollen, Eigentümer und Zustimmung.
Neue Teilnehmer pausieren; Notstopp benötigt keine Cloud. Parallele Aufnahmen
schreiben begrenzte FLAC-Segmente direkt auf Platte. Stop reiht jede Sitzung mit Audio
automatisch ein; andere Aufnahmen können während STT/LLM weiterlaufen. Der Recorder lädt keine
Whisper-/Torch-Modelle. Offline-STT verwendet multilingual small mit CPU/int8.
Der Broker bindet Ausgaben an Job, Lease, Guild und Eingabesnapshot. Auswertung
validiert JSON und Quellen; Rohtranskripte bleiben erhalten. Ergebnisse sind Entwürfe.

Ein gemeinsamer Bot-Prozess kennt die Daten seiner eingerichteten Guilds.
Guild-Trennung ist dort eine Anwendungsgrenze; sie ist keine eigene VM oder
Benutzerisolation pro Guild. Nachgelagerte Worker erhalten nur ihren konkreten Job,
eigene Schreibbereiche und keine fremden Guild-Daten. Kein Cross-Guild-RAG.

## Dokumentation

[Aufnahme und Protokollziele](docs/RECORDING.md), [Architektur](docs/ARCHITECTURE.md),
[Broker](docs/BROKER.md),
[Konfiguration](docs/CONFIGURATION.md), [Datenschutz](docs/PRIVACY.md),
[Betrieb](docs/OPERATIONS.md), [Restore](docs/RESTORE.md),
[Tests](docs/TESTING.md), [Upstream/Lizenzen](docs/UPSTREAM.md).

Containerrezepte, GitHub Actions und die lokale Ansible-Vorbereitung sind vorhanden.
Lokal ist keine Container-Engine verfügbar; OCI-/Isolationsnachweise fehlen.
Es wurden keine Images veröffentlicht und kein Serverdienst deployed. Ein
Steuerungsclient-Test registriert Commands, sendet aber keine Kanalnachrichten.
