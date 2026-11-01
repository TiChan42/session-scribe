# Daten, Zustimmung und Aufbewahrung

Aufnahme beginnt erst nach explizitem Start, Zustimmung aller aktuellen Teilnehmer
und sichtbar bestätigtem Aufnahmestatus. Beitritt pausiert; Neustart/Disconnect
muss unterbrechen. Keine heimliche Fortsetzung. Jede teilnehmende Person kann
lokal stoppen. Transport und Betrieb müssen dies zusätzlich praktisch testen.

Audio bleibt lokal. Ein ausdrücklich freigegebener Inferenzzugang sendet das
Transkript einschließlich Sprecherkennungen und gegebenenfalls Charakterzuordnung
an den Cloud-Anbieter. Vor Aktivierung sind Teilnehmerinformation, Vertraulichkeit,
Anbieterbedingungen, Speicherorte und vereinbarte Löschfristen zu klären.
Es gab bisher keine reale Aufnahme und keinen Cloud-Call dieser Anwendung.

Eine Discord-ID kennzeichnet eine Verbindung; bei gemeinsamem Mikrofon sind
Menschen nicht zuverlässig unterscheidbar. Keine Voiceprints, Embeddings oder
mandantenübergreifende Suche. Guilds erhalten getrennte beschreibbare Sitzungsspeicher und Budgets; Meeting
und DnD sind Modi derselben Bot-Identität. Der gemeinsame Recorder kennt seine
Guilds, nachgelagerte Worker erhalten nur einzelne Guild-Handoffs.

## Retention-Vorschlag, noch nicht aktiviert

- Audio: frühestens nach erfolgreicher STT, geprüftem Ergebnis und sieben Tagen.
- Rohtranskript/Ergebnis: beispielsweise 30 Tage, je Mandant ausdrücklich vereinbaren.
- Fehlerhafte/unterbrochene Jobs: keine automatische Löschung; Quotenalarm und
  manuelle Entscheidung, bevor die geschützte Reserve erschöpft ist.
- Backups: eigene vereinbarte Frist und Verfahren für betroffenenbezogene Löschung.

Es gibt noch keinen automatischen Retention-Worker; diese Fristen sind Vorschläge.
`delete` entfernt nach Berechtigungsprüfung die lokalen Sitzungsdateien, committete und verwaiste Job-Ergebnisdateien sowie
Metadaten im jeweiligen Store. Ein Infrastruktur-Löschauftrag muss zusätzlich
Evaluator-State, Handoff-Kopien, Backups und bereits zugestellte Discord-Dateien
erfassen. SQLite/WAL, Dateisysteme und Backups bieten keine garantierte physische
Überschreibung. Verschlüsselung und Schlüsselverwaltung gehören ins Betriebskonzept.

Rohdaten werden nicht durch Modellbereinigung ersetzt. Entwürfe enthalten
Quellenreferenzen; Zuständigkeit, Termin und Charakter bleiben ohne Beleg unbekannt.
Schema und Referenzprüfung ersetzen keine menschliche Plausibilitätskontrolle.

Logs enthalten nur Ereigniscodes, Jobzustand und Ressourcenmetrik; keine echten
Transkripte, Modellantworten, Tokens oder Produktionskonfiguration. Diagnosen,
Modellqualitätsproben und Reparatur-Workspaces verwenden synthetische Inhalte.

Der Broker erstellt begrenzte Audio-/Transkriptkopien und kann geprüfte STT-
Teilresultate für Wiederholungen zwischenspeichern. Diese Daten bleiben je
Mandant und Phase getrennt. Fehlerhafte Versuche bleiben bis zur expliziten
Lösch-/Aufbewahrungsentscheidung erhalten. Der übergreifende Löschauftrag für
Broker- und Evaluator-Caches ist noch nicht implementiert; lokale Recorder-
Löschung allein darf nicht als vollständige mandantenweite Löschung gelten.

Der Steuerungsbot verwendet sein eigenes Token; zusätzliche Aufnahme-Bots
benötigen getrennte Credentials in ihren Transportbereichen. Tokens werden
nicht pro Guild dupliziert und nicht an STT oder Auswerter weitergegeben. Guild-Administratoren konfigurieren ihren Server,
sehen jedoch weder das Bot-Token noch fremde Guild-Konfigurationen.
Konfigurations-/Statusantworten des Steuerungsclients sind private Interaktions-
antworten. Einrichtung liest Kanal-, Rollen- und Berechtigungsdaten der Guild;
Message-Content-/Member-/Presence-Intents bleiben aus. Das Starten des Clients
allein zeichnet nichts auf und führt keinen Cloud-Aufruf aus. Der inhaltsfreie
Heartbeat enthält keine Guild-/Nutzer-IDs oder Gesprächsinhalte.

Protokollkanäle sind eine bewusste Veröffentlichung des validierten LLM-Entwurfs
innerhalb der Guild. Teilnehmerinformation muss den Zielkanal und dessen Leserechte
abdecken. Je Sprachkanal kann Veröffentlichung abgeschaltet werden. Rohtranskripte
werden nicht stattdessen gepostet. Die persistente Outbox enthält nur Sitzung,
Ziel und Zustellstatus; sie wird bei lokaler Session-Löschung mit entfernt.

Ein lokaler Inferenz-Socket begrenzt den Netzwerkzugang des Auswerters; der
freigegebene Sitzungstext wird vom Hostgateway weiterhin an Ollama Cloud übergeben.
Job-Credentials sind kurzlebig und nur für eine exakt gebundene Anfrage gültig.
Das separate Quotaledger speichert Kontingente und verbrauchte Credential-Hashes,
keine Transkripte oder Modellantworten. Es muss privat gesichert werden; ein
Neustart darf Verbrauch oder Einmalverwendung nicht zurücksetzen.
## Manuelle Empfangsprobe

Der Betreiber kann einen Einzeltest für eine ausdrücklich zustimmende Person
vorbereiten. Ohne deren `/testaufnahme`-Command wird kein Sprachkanal betreten.
Der Command ist nur im festgelegten Protokollkanal und für diese Identität mit
Manage-Guild-Recht erlaubt. Startmeldung und Stoppknopf sind sichtbar; Verlassen
des Sprachkanals, Teilnehmerwechsel oder das Zeitlimit beenden die Aufnahme.
Die für die Probe bestätigte Cloud-Einwilligung wird in ihren privaten Metadaten
gespeichert. Eine vorbereitete Probe ist kein bestandener Empfangsnachweis.
