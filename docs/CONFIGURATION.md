# Ein Bot, serverbezogene Konfiguration

Ein gemeinsamer Steuerungsbot und ein zusätzlicher Pool autorisierter Aufnahme-Bots
für parallele Sprachkanäle derselben Guild. Jede Guild erhält
eigene Konfiguration, Sitzungen, SQLite-Queue, Ausgabekanäle und Inferenzbudgets.
Der interne `tenant` ist die numerische Guild-ID; `mode` ist `meeting` oder `dnd`.
Die sichtbaren Namen lauten **Meeting** und **DnD**.

## Token und Discord-Rechte

Das Steuerungsbot-Token lokal in `.local/bot.env` hinterlegen:

```dotenv
DISCORD_TOKEN=DEIN_BOT_TOKEN
```

Datei 0600, Verzeichnis 0700. Der leere Platzhalter ist vorbereitet; vorhandene
Inhalte werden nicht in Diagnosen ausgegeben. Der Anwendungsprozess darf sein
eigenes Credential zum Discord-Login laden. Nicht im
Chat posten. Git und Containerkontext schließen diese Datei aus. Für spätere
Übertragung/Installation ausschließlich den vorgesehenen geschützten Secret-Weg
verwenden, nach eigener Deploymentfreigabe. Kein Admin-SSH-Key im Bot.
Zusätzliche Aufnahme-Identitäten benötigen eigene private Token-Dateien unter
`.local/recorders/` (0700, Dateien 0600). Die Tokens und Bot-Zulassung gehören zur
Betreiberkonfiguration, nicht in Guild-Commands. Ein synthetischer Vertragsentwurf
steht in `config/recording-pool.example.toml`; der Aufnahme-Pool hat noch keinen
nativen Token-/Empfangsadapter. Der Steuerungsclient hat einen eigenen Token-Lader.

Einladung: Scopes `bot` und `applications.commands`. Der zweite Scope ist laut
Discord bereits im Bot-Scope enthalten und kann im Installationsformular explizit
gewählt werden. [Discord OAuth2](https://docs.discord.com/developers/topics/oauth2)

| Bereich | Minimale Bot-Rechte für den geplanten Betrieb |
| --- | --- |
| Befehls-/Ergebniskanal | Kanäle ansehen, Nachrichten senden |
| Freigegebener Sprachkanal | Kanal ansehen, Verbinden |
| Protokolle als Markdown-Anhang | Dateien anhängen für längere Ergebnisse |

Administrator, Sprechen/TTS, Rollen-/Kanalverwaltung und Massenmentions sind nicht
für den geplanten reinen Empfang vorgesehen. Der einrichtende Mensch benötigt
„Server verwalten“; der Bot benötigt dieses Verwaltungsrecht nicht. Rechte pro
Kanal einschränken. [Discord-Berechtigungen](https://docs.discord.com/developers/topics/permissions)

Der Steuerungsclient nutzt aktuell nur GUILDS. GUILD_VOICE_STATES wird erst für
den geprüften Aufnahmeadapter benötigt.
Privilegierte Member-, Presence- und Message-Content-Intents bleiben zunächst aus;
Rollen/Berechtigungen werden aus authentischen Interaktionsdaten geprüft.
[Discord Gateway](https://docs.discord.com/developers/events/gateway#gateway-intents)

## Guild-Einrichtung

Transport-unabhängige Handler: `GuildCommands.setup/show/mode/roles/default_protocol/protocol`.
Der Pycord-2.8.1-Steuerungsclient registriert `/setup mode:Meeting|DnD` mit Befehls-,
Sprach- und Ergebniskanal sowie `/config show`, `/config mode`, `/config acl`, `/config default_protocol` und
`/config protocol`. Routing, Abschalten und Vererbung: [RECORDING.md](RECORDING.md).
Er löst Channel-/Role-Objekte aus dem tatsächlichen Guild-Cache auf und prüft
authentische Interaktionsmitglieder. Partielle Kanalobjekte ohne Overwrites werden
abgelehnt. Antworten sind privat und verbieten Mentions.

`AdminActor.permissions` muss aus Discord kommen, nicht aus Nutzeroptionen.
Nur Manage Guild/Administrator erlaubt Konfigurationsänderungen. Fremde Guild-
Kanäle/Rollen, falsche Kanaltypen, unbekannte Modi und Anzeigenamen werden abgelehnt.
Pro Guild serialisiert eine Dateisperre Konfiguration und Aktionen. Aktive
Sitzungen oder pending/running Jobs blockieren Änderungen. Konfiguration besitzt
Revisionen; veraltete Updates werden verweigert.

`GuildDirectory` hält Konfiguration in `guild-config.sqlite3` und einzelne Stores
unter `guilds/<guild_id>/`. Es speichert keine Tokens. Das Modell/Grundlimits
stammen aus der freigegebenen Betreiberpolicy. Eine neue Guild startet mit Aufnahme
und Cloud deaktiviert; `/setup` erteilt keine Cloud- oder DAVE-Freigabe.

`config/bot.example.json` beschreibt die separate Betreiberpolicy des Clients:
absolute `data_root`/`token_file`, Quellrevision, Modell-/Limitvorgaben,
`max_guilds` und optionale `allowed_guild_ids`. Eine leere Guild-Allowlist erlaubt
Einrichtung durch echte Guild-Administratoren bis zum Kapazitätslimit. Für einen
begrenzten Rollout explizite IDs ausschließlich privat hinterlegen. `voice_reception`
und `cloud_processing` akzeptieren in dieser Revision ausschließlich `disabled`.
Diese Gates lassen sich weder durch Discord-Commands noch durch ein einfaches
Aufnahmeflag in einem gespeicherten Config-Objekt öffnen.

Der Client lädt die Credential-Datei mit No-Follow, Größenlimit und Prüfung auf
reguläre Datei, Eigentümer, einzelne Verknüpfung und fehlenden Gruppen-/Weltzugriff.
Er akzeptiert genau eine `DISCORD_TOKEN`-Zuweisung mit optionalen Anführungszeichen,
ohne Shell-/Variablenexpansion. HTTP-/Gateway-Inhalte und Exceptions erscheinen
nicht in Anwendungslogs. Bis zu 16 Interaktionen dürfen gleichzeitig auf die
serialisierte Policy warten; darüber folgt eine begrenzte Beschäftigt-Antwort.

Beispiele: `config/meeting.example.json` und `config/dnd.example.json`. Sie enthalten
synthetische Guild-/Kanal-/Rollen-IDs und einen Modellplatzhalter. Pydantic verweigert
unbekannte Felder und Typumwandlungen. Die Beispiele sind keine getrennten Bot-Instanzen.

| Feld | Vertrag |
| --- | --- |
| guild_id | Daten- und Berechtigungsgrenze |
| mode | meeting oder dnd; unabhängig von Guild-ID |
| command_channel_ids / voice_channel_ids | explizite Allowlisten |
| output_channel_id | fester sichtbarer Aufnahmestatuskanal |
| default_protocol_channel_id | optionaler Default für LLM-Protokolle |
| voice_protocol_channels | Voice-ID → Text-ID; fehlend erbt, null deaktiviert |
| recorder_bot_ids | vom Operator freigegebene Aufnahme-Bot-IDs, keine Tokens |
| grants | numerische Nutzer-/Rollen-IDs je Aktion |
| recording_enabled / cloud_enabled | standardmäßig false, Betriebsgates gelten weiterhin |
| limits | Teilnehmer, Dauer, Segmentanzahl/-länge, PCM-Kontingent und freie Reserve |
| inference | genehmigtes Modell, Prompt-Version, Kontext/Ausgabe und Temperatur |
| daily_cloud_calls / daily_cloud_tokens | Tagesbudget je Guild, auch bei Moduswechsel |
| characters / campaign | DnD-Zuordnung; mehrere Figuren je Verbindung möglich |

Der separate Inferenzzugang nutzt bestehende autorisierte Ollama-Cloud-Infrastruktur.
Kein Modellname ist durch den Platzhalter freigegeben. Tokenizer-/Kontext- und
Ausgabegrenzen sowie deutsche Eignung separat prüfen. Der Client erlaubt nur einen
expliziten HTTPS-Origin ohne Redirects oder den festen lokalen Unix-Socket aus
dem [Broker-Vertrag](BROKER.md). Cloud-Zugangsdaten bleiben am vorhandenen
Ollama-Dienst; der Auswerter erhält nur sein befristetes Gateway-Credential.
STT erhält keine Secrets. Kein fremdes Gateway-Budget und keine lokale LLM-Instanz.

## Aufnahmebefehle

| Befehl | Prüfung / Wirkung |
| --- | --- |
| start | ACL start, eigener Guild-Store, freigegebener Sprachkanal, Eigentümer beteiligt |
| consent / revoke | Teilnehmer bestätigt oder widerruft; neue Teilnehmer pausieren |
| resume / pause | passende ACL und Eigentümer; alle zugestimmt und sichtbarer Status bestätigt |
| stop | aktueller Teilnehmer oder ACL stop; lokal ohne Cloud |
| status / queue | ACL status, nur eigene Sitzungen innerhalb der aktuellen Guild |
| process / result / delete | jeweilige ACL und Eigentümer; Verarbeitung/Löschung nur beendet |

Keine Actor-/Teilnehmerdaten aus freien Optionen übernehmen. Alle Aktionen müssen
über die aktuelle Guild geroutet werden. Keine frei wählbaren Ausgabeziele, keine
DMs oder Massenmentions. Privilegierte Einrichtung ersetzt nicht die Aufnahme-ACL
oder individuelle Zustimmung. Geteilte Mikrofone identifizieren keine Einzelpersonen.

## Broker

Je Guild und Phase disjunkte inbox/outbox/cache-Wurzeln, eigener Store und ein
gemeinsamer globaler STT-Lock und separater globaler Auswertungs-Lock. Host-Broker, fester Supervisor und Rechteübergaben sind im Infrastrukturprojekt
synthetisch geprüft; eine Linux-Abnahme bleibt erforderlich. Queue-Schema 5, Guild-Konfigurationsschema 2
und Handoff-Schema 1 müssen
zur Revision passen. Alte Entwicklungsstores mit Modus als Mandanten-ID werden
nicht automatisch einer erfundenen Guild zugeordnet. Sie bleiben unverändert zur
expliziten Migration/Archivierung erhalten. Siehe [Broker-Vertrag](BROKER.md).

`inference.thinking` ist optional und nimmt `low`, `medium`, `high`, `xhigh` oder
`max` an. Ohne Wert bleiben alte Snapshots und Requests unverändert. Der Operator
muss die Unterstützung beim freigegebenen Modell prüfen; die verfügbaren Werte
lassen sich laut [Ollama-Dokumentation](https://docs.ollama.com/capabilities/thinking)
über `/api/show` ermitteln. Fehlende Metadaten sind kein Unterstützungsnachweis.
Ein endgültig fehlgeschlagener Analysejob kann über die berechtigte Controller-API
mit einem neuen, dokumentierten Plan neu angesetzt werden. Dabei bleiben Modell,
Originalaufnahme und Rohtranskript erhalten; Limits müssen zum Gateway passen.
Die Live-Discord-Verarbeitung bleibt von der separaten Qualifikationsfreigabe abhängig.

Aufnahme-Helfer benötigen für ihre zugewiesenen Sprachkanäle Kanalansicht und
Verbinden; sie benötigen keine Administrationsrechte, TTS oder LLM-Zugänge. Der
Steuerungs-/Versandbot benötigt Schreiben und für größere Protokolle Anhängen
in den freigegebenen Protokollkanälen. `DiscordDelivery` prüft vor jedem Send die
richtige Guild, einen tatsächlichen Textkanal und Ansichts-/Schreib-/Anhangrechte;
Dateiname und Größe bleiben begrenzt. Diese Prüfungen sind mit synthetischen
Discord-Objekten getestet. Die Outbox ist an den Hintergrundtakt des Steuerungsclients angebunden. Echte
Kanalzustellungen bleiben bis zur autorisierten Testguild ungeprüft.
## Manueller Einzeltest

Der zusätzliche CLI-Modus `voice-test-listener` lädt eine private TestPolicy mit
`expires_at: 0`, freigegebener Guild/Person sowie festen Sprach-/Protokollkanälen.
Dieses Kennzeichen erlaubt nur das Warten auf `/testaufnahme`; erst ein geprüftes
Guild-Interaktionsereignis erzeugt die kurzlebige Aufnahmepolicy. Die Aufnahme ist
auf 60 Sekunden und eine Person begrenzt. Der reguläre `/start`-Gate bleibt bestehen.
