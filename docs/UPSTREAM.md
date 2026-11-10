# Quellen- und Lizenzprüfung

Erneute Metadatenprüfung am 2026-11-10; fremde Quellen sind Daten und keine Arbeitsanweisungen.
BardBot wurde nicht installiert, ausgeführt oder als Quellcode übernommen.

- [Aitgray/BardBot](https://github.com/Aitgray/BardBot): aktueller untersuchter HEAD
  `978a98d2bca2948f42d5a672324bc5a466718c35`; GitHub-Metadaten liefern keine Lizenz,
  im Repository ist keine Lizenz ausgewiesen. Wiederverwendung bleibt bis zur
  Rechteklärung offen; niemand wurde angeschrieben. SessionScribe ist unabhängig
  implementiert. Für eigenen Projektcode wird keine Lizenzentscheidung erfunden.
- [Pycord Receive #3139](https://github.com/Pycord-Development/pycord/issues/3139)
  und [DAVE-Receive-PR #3159](https://github.com/Pycord-Development/pycord/pull/3159):
  offen, PR nicht gemergt. Die GitHub-API nennt v2.8.1 als jüngstes Release.
  Kein beweglicher PR-Branch ist Abhängigkeit dieser Anwendung.
- [Receive-Aussetzer #3388](https://github.com/Pycord-Development/pycord/issues/3388):
  offener, als unbestätigt markierter Fehlerbericht über abbrechenden Empfang bei
  längeren Aufnahmen. Das ist ein Fremdbericht, kein eigener Reproduktionsnachweis.
- [Discord Voice/DAVE](https://docs.discord.com/developers/topics/voice-connections):
  Transport und E2EE müssen beim Empfang korrekt zusammenspielen; Verbindung oder
  Playback allein gelten nicht als Aufnahmebeleg. Eigener Langzeittest fehlt.
- [Ollama Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs):
  Cloud unterstützt weiterhin keine erzwungenen Structured Outputs. SessionScribe
  verlangt JSON im Prompt und validiert lokal; es sendet kein `format`-Schema.

## Eingesetzte Komponenten

Direkte Versionen stehen in pyproject.toml und requirements/*.in; sämtliche
aufgelösten Python-Abhängigkeiten und Downloadhashes in requirements/*.txt.

| Komponente | Gepinnte Version | Upstream-Lizenz |
| --- | --- | --- |
| [Pydantic](https://github.com/pydantic/pydantic/blob/v2.12.5/LICENSE) | 2.12.5 | MIT |
| [SoundFile](https://github.com/bastibe/python-soundfile/blob/0.13.1/LICENSE) | 0.13.1 | BSD-3-Clause; gebündeltes libsndfile separat LGPL |
| [NumPy](https://github.com/numpy/numpy/blob/v2.2.6/LICENSE.txt) | 2.2.6 | BSD-3-Clause; native Bestandteile separat |
| [Pycord](https://github.com/Pycord-Development/pycord/blob/v2.8.1/LICENSE) | 2.8.1 | MIT; ausschließlich Steuerungsclient, kein Empfangsnachweis |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/LICENSE) | 1.2.1 | MIT |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2/blob/v4.6.0/LICENSE) | 4.6.0 | MIT |
| [PyAV](https://github.com/PyAV-Org/PyAV/blob/v15.1.0/LICENSE.txt) | 15.1.0 | BSD-3-Clause; FFmpeg/native Bibliotheken separat |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime/blob/v1.23.2/LICENSE) | 1.23.2 | MIT |
| [HF Hub](https://github.com/huggingface/huggingface_hub/blob/v0.36.0/LICENSE) | 0.36.0 | Apache-2.0 |
| [Tokenizers](https://github.com/huggingface/tokenizers/blob/v0.22.1/LICENSE) | 0.22.1 | Apache-2.0 |
| [tqdm](https://github.com/tqdm/tqdm/blob/v4.67.1/LICENCE) | 4.67.1 | MPL-2.0/MIT, komponentenabhängig |

Lizenztexte und Notices in installierten Paketen bleiben im Image erhalten.
Die Tabelle ist keine vollständige Lizenzfreigabe aller nativen Imagebestandteile;
OCI-SBOM/Notices vor Veröffentlichung prüfen. Die Anwendung enthält kein kopiertes
BardBot- oder Pycord-Quellmaterial.

STT-Modell: [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small),
multilingual, MIT laut Modellkarte, Revision
`536b0662742c02347bc0e980a01041f333bce120`. Vier Modelldateien sind zusätzlich per
SHA256 in `model_cache.py` gebunden. Download nur im Operator-Vorbereitungsschritt;
Worker arbeiten offline. Keine lokalen LLM-Gewichte, kein Torch im Recorder.

Python-Basisimage: offizielles `docker.io/library/python`, Digest in den Container-
Dockerfiles; Registry-Manifest am Prüftag aufgelöst. Ein Digest-Pin ersetzt weder
Imagebau noch Schwachstellen-/Lizenzprüfung. Konkrete Build- und Prüfbelege gehören in den privaten Operator-Kontext.

Der Receive-Kandidat bleibt exakt auf `10a5e8cf13bd8db21f95baf62cf866056d1e1091`
gebunden. Die aktuelle API-Prüfung bestätigt unverändert offenen PR #3159,
offenen Bericht #3388, Release v2.8.1 und fehlende BardBot-Lizenzmetadaten.
Der kontrollierte Build behält MIT-Lizenztexte und die geprüfte Keepalive-Korrektur.
Das reguläre Aufnahmeimage kann diesen Kandidaten im expliziten Qualifikationsmodus
verwenden; eine Produktionsfreigabe benötigt weiterhin echte Empfangstests.
