# Backup und Wiederherstellung

Git enthält Code und Konfigurationsbeispiele, keine Audios, Datenbanken oder
Runtime-Secrets. Ein externes verschlüsseltes Backupziel und dessen Zeitplan sind
noch festzulegen. Es ist kein laufendes Offsite-Backup eingerichtet worden.

Für einen konsistenten vollständigen Stand Recorder geordnet stoppen, Worker
abschließen/stoppen und automatische Jobübernahme sperren. `Store.backup(path)`
verwendet die SQLite-Backup-API, sodass ein einzelnes DB-Abbild auch mit WAL
konsistent ist. Dieses Abbild allein enthält keine Audio-/Ergebnisdateien und
ersetzt keinen quieszierten Gesamtsnapshot.

Schema 5 umfasst auch `analysis_plans` und `retired_jobs`; diese Tabellen gehören
zum konsistenten DB-Abbild. Lokale Migrationskopien in `migration-backups/` sind
privat zu behandeln und ersetzen kein verschlüsseltes externes Backup. Ein
Downgrade auf einen Stand vor Schema 5 erfordert dessen passenden DB-/Dateistand.
Vor einer erneuten Veröffentlichung nach Restore bereits vorhandene Discord-
Nachrichten und Zustellbelege abgleichen; Quotenledger nicht zurücksetzen.

Je Mandant getrennt sichern: Recorder-Dateibaum und konsistente DB, Evaluator-
Zustand/Budget, vollständige Handoffs und Checkpoints, committierte `results/`-Bäume samt `job_results`-Tabelle,
Konfiguration, Runtime-
Secrets über getrennten geschützten Weg sowie Revision und OCI-Digests. Den
read-only STT-Modellcache kann man anhand gepinnter Revision/Hashes erneut aufbauen.
Backups dürfen keine Symlinks in fremde Mandanten verfolgen. Metadaten und Rechte
bewahren; verschlüsselt außerhalb des Hosts ablegen und Erfolg/Alter überwachen.
Evaluator-Zustand wird je Guild und logischem Job gesichert. Das unabhängige
Gateway-Ledger enthält die autoritativen kumulierten Guild-/Globalbudgets.

Restore zunächst in leere private Testverzeichnisse ohne ausgehendes Netz und
ohne echte Tokens. Passende Quell-/Imageversion und Schema prüfen; gesicherte
Dateien/DB mit korrekten Eigentümern zurückspielen. SQLite integrity_check,
Session-/Manifest-/Audiohashes und aktive Jobzustände prüfen. Abgelaufene Jobs
nur nach nachgewiesenem Ende alter Worker freigeben. Aktive Aufnahmen als
unterbrochen markieren; nie automatisch aufzeichnen. Budget und unklare
Zustellungen nicht auf null zurücksetzen, sonst drohen zusätzliche Kosten/Duplikate.

Der fokussierte Test stellt ein synthetisches DB-Backup wieder her und prüft
Mandantenidentität sowie Queue-/Sessionzustand. Ein vollständiger verschlüsselter
Offsite-Restore einschließlich Daten, Berechtigungen, Secrets und neuem Host ist
noch nicht ausgeführt. Erst danach darf eine Produktionssicherung als geprüft gelten.

Konformes Schema 1 → 4 wurde mit einem synthetischen Altstand einschließlich Jobs und
Sitzungen geprüft. Vorwärtsmigration ergänzt Ergebnisbelege, ohne vorhandene
Daten zu löschen. Ein Downgrade ist nicht vorbereitet. Bei Restore müssen
`job_results` und die referenzierten Ergebnisdateien zusammenpassen; Leser
verweigern Hashabweichungen. Verwaiste Dateien ohne DB-Commit sind keine
erfolgreichen Aufträge. Broker-Caches und Stage-Identitätsmarker getrennt je
Mandant wiederherstellen, niemals zwischen Instanzen umwidmen.

Zusätzlich den gemeinsamen `guild-config.sqlite3`-Stand und die getrennten Steuerungs-/Aufnahme-Bot-Secrets über getrennte geschützte Wege sichern. Stores gehören anhand numerischer
Guild-ID zu ihrem Server, nicht anhand ihres Modus. Alte Entwicklungsdateien
werden ohne explizite Guild-Zuordnung nicht automatisch übernommen.
Die Betreiberpolicy des Steuerungsclients separat sichern; sie enthält nur
Credential-Referenzen. `control-health.json` ist ein kurzlebiger Laufzeitbeleg
und kein zu restaurierender Bereitschaftsnachweis. Nach Wiederherstellung erst
frische Command-Registrierung derselben Revision abwarten. Voice/Cloud bleiben
bis zu ihren eigenen Freigaben deaktiviert.

Protokoll-Outbox und Zustellungsbelege gemeinsam sichern. Nach Restore externe
Discord-Zustellung vor automatischem Versand abgleichen; ein alter Backupstand
kann bereits gesendete Protokolle noch als pending enthalten. Aktive Aufnahme-
Bot-Slots freigeben erst nach nachgewiesener Beendigung alter Verbindungen.

Das eigene Gateway-Quotaledger einschließlich Einmalverwendungsbelegen separat
und konsistent sichern. Befristete Job-Grants und Tokens nach Restore verwerfen;
erst nach nachgewiesenem Ende alter Worker und neuem gebundenen Auftrag ersetzen.
Keine fremden Gateway-Budgets übernehmen oder einen verbrauchten Versuch erneut
freischalten. Dieser Gesamt-Restore ist noch nicht praktisch geprüft.
