# Betrieb und Freigabegates

Aktueller Stand: lokal implementierter Kern und lokale Deploymentvorbereitung.
Kein produktiver Serverdienst, kein Live-DAVE-Test,
Native Worker-Supervision und Hostrolle sind als Quellcode vorbereitet;
ein freigegebener, laufender Inferenzzugang besteht noch nicht.
Der Discord-Steuerungsclient registriert Slash Commands; die Recorder-Container-
Vorgabe startet `bot --policy /config/bot.json` und kann keine Live-Aufnahme aktivieren.
Eine echte private Betreiberpolicy und ein eigenes Token sind erforderlich.

Der Steuerungsclient schreibt einen inhaltsfreien Heartbeat nach
`control-health.json`. `control-health --root /data --revision <commit>` prüft
Registrierung, exakte Revision und höchstens 65 Sekunden Alter; veraltete oder
zukünftige Quittungen gelten als nicht bereit. Voice/Cloud bleiben false.
Shutdown setzt die Control-Bereitschaft zurück und schließt Client und SQLite.
Das ist kein DAVE-, Worker-, Cloud- oder Host-Isolationsnachweis.

## Infrastrukturvorbereitung

Host-Provisioning ausschließlich im separaten Infrastrukturprojekt. Dessen Regeln,
Onboarding und Zielinventory zuerst lesen. Die neue `sessionscribe-prepare.yml`
läuft ausschließlich lokal und rendert einen deaktivierten Plan nach `.local/`.
Sie weist keine IDs/Ports zu und ändert keine Workload-Liste oder Dienste.
`isobox_service`/`isobox_registry` wurden geprüft; ihre aktuelle Identitätsliste
und HTTPS-Regeln decken Voice und isolierte Stage-Handoffs noch nicht ab.

Ein späterer Apply muss getrennte Recorder-/STT-/Evaluator-Benutzer, Sub-IDs
und Stage-Quoten anlegen. Guild-Dateibäume und Job-Mounts bleiben getrennt;
der gemeinsame Recorder benötigt Zugriff auf seine Guilds. Bestehende Zuordnungen erhalten
und additive Registry-/Firewall-Erweiterungen verwenden. `enabled=false` überspringt
Management, `deploy=false` stoppt mit Datenerhalt. Diese entfernte Apply-Semantik
ist noch nicht implementiert; der lokale Plan darf nicht als Abschaltung gelten.

Vor Reservierung: UID/GID/Sub-IDs, belegte Ports, aktuelle RAM-/CPU-/Diskreserve,
Images, Modellcache, parallele Aufnahmen und Rollback zusammen prüfen.
PCM16 Stereo 48 kHz benötigt vor Kompression rund 0,644 GiB je Sprecherstunde;
FLAC variiert. Das Default-Kontingent begrenzt pro Sitzung auf 1 GiB konservative
PCM-Zählung, nicht auf garantierte vier Stunden. Alle vorgesehenen Stage-Datenträger mit Guild-Kontingenten,
1 GiB Modellcache und Image-/Rollbackplatz zählen neben Hostreserve.

STT startet mit 2560 MiB und zwei Kernen, Ressourcen-Admission fordert mindestens
3584 MiB verfügbar, 2 GiB freie Workerablage und begrenzte Last. Diese Ausgangswerte
müssen auf dem Zielhost unter Last gemessen werden. Der Broker verlangt vor
Übernahme die Bestätigung verwaister Worker und
nach jeder Ausführung bestätigte Beendigung. Ein Host-Lock bleibt bis dahin gehalten;
Timeout muss die gesamte Cgroup beenden. Native Nachweise fehlen noch.

## Isolation und Netzwerk

Rootless/non-root, read-only root, managed AppArmor OCI-Hook, explizites Seccomp,
NNP, leere Capabilities und äußere/innere RAM-/CPU-/PID-/Disk-/Temp-/Loglimits.
Keine Hostnetze, Engine-Sockets, privilegierten Container oder Admin-Keys.
Worker-Argv ist ein prüfbarer Vertrag, kein bereits installierter Launcher.

Recorder braucht HTTPS/WSS und DNS plus explizit getestete, kurzlebige Regeln
für genau die ausgehandelten Voice-Zieladressen/-UDP-Ports und Antworten. IPv4
und IPv6 prüfen; keine pauschale UDP-Freigabe. Private Netze, Hostdienste,
Metadaten und andere Mandanten müssen gesperrt bleiben. STT hat `network=none`;
Evaluator ausschließlich den eigenen Inferenzzugang. Zielwechsel/ablaufende
Voice-Regeln müssen bei Unsicherheit pausieren, nicht die Firewall öffnen.

## CI und spätere Auslieferung

GitHub Actions prüft PRs ohne echte Credentials. Actions sind per Commit gepinnt,
Rechte minimal, Checkout ohne gespeicherte Credentials. Kein `pull_request_target`.
Der Buildjob verlangt aktuelles geschütztes main, einen dort gemergten PR und
erfolgreiche `verify`-/`host-core`-Prüfungen genau dieser Revision. Branchschutz
bleibt zusätzlich eine Repositoryeinstellung; YAML allein beweist ihn nicht.

Der serialisierte Releasejob veröffentlicht drei unveränderliche OCI-Archive,
Manifeste mit vollständiger Imagenamens-/Digestbindung und Python-SBOMs als
öffentliche GitHub-Release-Assets. Er prüft Archivpfade, jeden Blob-Hash, Quellrevision,
Non-Root-Benutzer, Entry-Point und eingebettete Credential-Variablen. GitHub-OIDC
attestiert die Artefakte über den fest gepinnten offiziellen Provenance-Action.
Nur dieser Main-Job erhält kurzlebige repo-begrenzte Veröffentlichungsrechte.
PR-, Fork-, Tag- oder manuelle Events bekommen diese Rechte nicht.

Ein separates signiertes `host-core.tar` enthält die Anwendung als Wheel und genau
die fünf hash-gepinnten Core-Abhängigkeiten für Linux x86_64/Python 3.14. Der Host
installiert sie offline in ein eigenes unveränderliches Release-Venv; Audio-,
Discord- und Inferenzbibliotheken gehören nicht in diesen Host-Core. Das Bundle
verweigert private Wheel-Inhalte und unvollständige oder ungepinnte Abhängigkeiten.

Die Hostauslieferung muss Signatur, Signer-Workflow, Hosted Runner, `refs/heads/main`,
Quellrevision und aktuell erfolgreiche CI verifizieren, bevor sie ein Artefakt
importiert. Öffentliche Assets und beigelegte Sigstore-Bundles ermöglichen das
ohne dauerhaften GitHub-Zugang auf dem Server. Hostinstallation, OS-SBOM,
Schwachstellenprüfung und tatsächliche Containerabnahme bleiben eigene Gates.

Der Host-Updater prüft merged/protected-main und monotone Revision,
verwirft veraltete Deployments, versucht begrenzte Wartungsmeldung, schließt aktive
Aufnahmen und startet sie nach Update nicht erneut. Secrets, Queues und Daten
bleiben erhalten. Readiness muss genau die neue Revision bestätigen; bei Fehler
vorherigen Digest samt kompatibler Konfiguration wiederherstellen. DB-Migrationen
benötigen eigenen Rückweg. Dieser Rollback ist noch nicht praktisch getestet.

## Health-/Update-/SBOM-Vertrag

Ein späterer Hostcollector liefert nur nichtinhaltliche Felder: schema_version,
revision/image_digest, tenant/stage, state, active_recording, queue_pending,
oldest_job_age, last_success, incomplete_chunks, disk_reserve_ok und schema_version_db.
Readiness des Recorders bleibt false, solange DAVE/UDP/ACL-Funktionstests fehlen.
Die separat ausgewiesene Control-Bereitschaft kann bei geschlossenem Voicegate true sein.
Liveness darf keine Aufnahme oder Modellanfrage auslösen. Umfangreiche periodische
Prüfungen höchstens stündlich; gezielte Tests nach Änderungen separat.

`python -m sessionscribe.sbom` erzeugt eine deterministische CycloneDX-Liste der
installierten Python-Komponenten. OS-Pakete und native Bibliotheken müssen durch
den vorhandenen OCI-Scanner ergänzt werden. Registryrevision und SBOM gehören zur
Updatefreigabe. Reparatur-Workspaces erhalten nur öffentlichen Code und synthetische
Fixtures; keine Produktionsgespräche oder Secrets, keine zusätzlichen Hostrechte.

## Störung

Bei Ressourcenmangel Queue stehen lassen; keine höhere Priorität als Sicherheits-
oder Bestandsdienste. Bei Cloud-Ausfall Rohtranskript lokal erhalten und Budget/
Retry-Grenzen beachten. Invalide/abgeschnittene Antworten nie veröffentlichen.
Bei sending/uncertain-Zustellung festen Zielkanal manuell abgleichen; nicht blind
wiederholen. Offene Audios nur als unterbrochen prüfen, Originale behalten.

## Broker-Recovery und Schemawechsel

Vor dem ersten späteren Hostlauf Schema-1-Stores konsistent sichern. Schema 4
fügt transaktionale Ergebnisbelege hinzu. Worker erhalten ausschließlich einen
gebundenen Snapshot und eigenen Ausgabepfad. Eine erfolgreiche Rückkehr des
Starterprozesses reicht nicht: Runner muss alle Worker-/Hilfsprozesse abgleichen.
Bei unklarer Beendigung bleibt der Job running; keine manuelle Lease-Freigabe,
bevor die alte Cgroup nachweislich leer ist.

Crash vor Ergebnis-Commit: rohe Versuchsdateien sind ohne gültigen DB-Zeiger
nicht abgeschlossen. Validierte STT-Checkpoints können nach Beendigung erneut
verwendet werden. Veränderte Session-/Modell-/Transkript-Snapshots werden bei
Wiederholung abgelehnt. Alte Eingabekopien zählen zum Diskbudget; bei Platzmangel
keine automatische Löschung fehlgeschlagener Daten. Siehe [BROKER.md](BROKER.md).

## Parallele Aufnahme und Publikation

Der Operator verwaltet Aufnahme-Identitäten, Token-Dateien und erlaubte Guilds.
Jeder Empfänger erhält nur sein eigenes Credential. Bots dürfen pro Guild nur
einen Voice-Kanal belegen; zusätzliche Kanäle benötigen weitere Identitäten.
Globale Capture-Limits (Sessions, Spuren, reservierte Bytes und freie Diskreserve)
gelten zusätzlich zu Guild-/Sessionquoten. Die reservierte Disk muss auch wartende
Audios und Wiederholungs-Handoffs tragen. Volle Disk führt zum Stop/Admission-
Warten, nicht zu einem Wechsel auf RAM oder automatischer Rohdatenlöschung.

Stop schließt und queued lokale Audios ohne Cloud-Abhängigkeit. Hintergrund-
Scheduler arbeitet Guild-Queues fair ab; STT bleibt global seriell. Erst validierte
LLM-Resultate erzeugen Protokoll-Outbox-Einträge. Pending/Sending-Outbox blockiert
umlenkende Konfigurationsänderungen. Uncertain-Sendungen manuell abgleichen.
Schema 4 und Guild-Konfigurationsschema 2 sind bei Readiness/Restore zu beachten.
Native Aufnahme-/IPC-/Token-/Bot-Pool-Integration und echte Versandtests sind offen.

## Native Adaptervorbereitung

Der native Runner liegt im Infrastrukturprojekt und ist synthetisch geprüft.
Die Host-Broker-Anbindung ist implementiert, aber noch nicht provisioniert. Auswertung
verwendet einen eigenen globalen Lock und kleinere Admission-Grenzen (768 MiB
verfügbaren RAM, 128 MiB freie Stage-Ablage). Der Unix-Inferenzgateway hat ein
eigenes persistentes Kontingent und kann den Auswerter mit `network=none` bedienen.
Job-Credential-Ausstellung und Rechteübergaben sind synthetisch geprüft; die
tatsächlichen Dienste und Linux-Eigenschaften müssen vor Aktivierung geprüft werden.

## Späteres Feature: automatische Dependency-Reparatur

Der Betreiber hat Dependency-Reparaturen und GitHub-PR-Erstellung durch den
Server-Manager ausdrücklich zurückgestellt. Dafür jetzt keine GitHub-Credentials,
Deploy-Keys, Reparaturrechte oder automatischen PR-Workflows provisionieren.
Die vorhandene lokale Git-/GitHub-Anmeldung dient der manuellen Entwicklung
und wird nicht auf den Server kopiert. Der lesende Management-Bot bleibt lesend.

Ein späterer Auftrag muss repositorybeschränkte Serverberechtigung, isolierte
Reparaturkopien mit synthetischen Tests und eigene Branches von geschütztem main
zurück nach main umsetzen. Keine Produktionssecrets/-gespräche in Workspaces,
keine direkten Main-Pushes und kein automatischer Merge. Health/SBOM-Anbindung
ist davon getrennt und bleibt Bestandteil der Deploymentvorbereitung.
