from __future__ import annotations

import re
from typing import Protocol

from .analysis import validate_analysis
from .models import Analysis, Config, Segment
from .store import Conflict, Store
from .transcript import load_transcript


def safe_text(text: str) -> str:
    # Escape mentions, Markdown links, code fences and formatting from transcript/model output.
    text = text.replace("@", "@\u200b").replace("\x00", "")
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    return re.sub(r"([\\`*_{}\[\]()<>#!|~])", r"\\\1", text)


def _alias_roster(speaker_aliases: dict[str, str] | None) -> list[str]:
    if not speaker_aliases:
        return []
    labels = sorted(set(speaker_aliases.values()), key=str.casefold)
    return ["## Sprecher-Aliase", "", *[f"- {safe_text(label)}" for label in labels], ""]


def _source_references(
    source_ids: list[str],
    speaker_aliases: dict[str, str] | None,
    segments: list[Segment] | None,
) -> str:
    segment_by_id = {segment.id: segment for segment in segments or []}
    rendered = []
    for source_id in source_ids:
        segment = segment_by_id.get(source_id)
        alias = (speaker_aliases or {}).get(segment.speaker_id) if segment else None
        rendered.append(f"{source_id} ({safe_text(alias)})" if alias else source_id)
    return ", ".join(rendered)


def render_analysis(
    result: Analysis,
    speaker_aliases: dict[str, str] | None = None,
    segments: list[Segment] | None = None,
) -> str:
    lines = [
        f"# SessionScribe {'Meeting' if result.mode == 'meeting' else 'DnD'}",
        "",
        "Automatischer Entwurf – Quellen prüfen.",
        "",
    ]
    lines.extend(_alias_roster(speaker_aliases))
    for finding in result.findings:
        lines += [
            f"## {finding.kind}",
            "",
            safe_text(finding.text),
            "",
            "Begründung: " + safe_text(finding.rationale),
            "",
            "Quellen: " + _source_references(finding.sources, speaker_aliases, segments),
            "",
        ]
        if finding.kind == "task":
            lines += [
                f"Zuständig: {safe_text(finding.owner or 'unbekannt')}",
                f"Frist: {safe_text(finding.deadline or 'unbekannt')}",
                "",
            ]
        if finding.category:
            lines += [
                f"Kategorie: {finding.category}",
                f"Figur: {safe_text(finding.character or 'unklar')}",
                "",
            ]
    if result.justified_mermaid:
        lines += ["", "## Diagramm", "", "```mermaid", result.justified_mermaid, "```", ""]
    return "\n".join(lines)


def render_interpretation(
    result: Analysis,
    speaker_aliases: dict[str, str] | None = None,
    segments: list[Segment] | None = None,
) -> str:
    lines = [
        f"# Interpretierte Zusammenfassung – {'Meeting' if result.mode == 'meeting' else 'DnD'}",
        "",
        "Einordnung aus dem Gesprächskontext; Schlussfolgerungen bleiben Interpretationen.",
        "",
        "## Zusammenfassung",
        "",
        safe_text(result.interpretation.summary),
        "",
    ]
    lines.extend(_alias_roster(speaker_aliases))
    if result.interpretation.issues:
        lines += ["## Problematische oder unklare Stellen", ""]
        for issue in result.interpretation.issues:
            lines += [
                safe_text(issue.text),
                "",
                "Quellen: " + _source_references(issue.sources, speaker_aliases, segments),
                "",
            ]
    else:
        lines += ["## Problematische oder unklare Stellen", "", "Keine erkannt.", ""]
    if result.interpretation.mermaid:
        lines += ["## Kontextdiagramm", "", "```mermaid", result.interpretation.mermaid, "```", ""]
    return "\n".join(lines)


MAX_PROTOCOL_BYTES = 2_000_000


class Delivery(Protocol):
    def send(self, channel_id: str, content: str, *, allowed_mentions: dict) -> str: ...

    def send_file(
        self, channel_id: str, filename: str, data: bytes, *, allowed_mentions: dict
    ) -> str: ...

    def send_files(
        self, channel_id: str, files: dict[str, bytes], *, allowed_mentions: dict
    ) -> str: ...


def _publish(
    store: Store, sid: str, channel_id: str, configured_channel_id: str, delivery: Delivery
) -> bool:
    s = store.session(sid)
    if (
        s.protocol_channel_id is None
        or channel_id != configured_channel_id
        or channel_id != s.protocol_channel_id
    ):
        raise PermissionError("protocol channel denied or posting disabled")
    segments = load_transcript(store, sid)
    result = validate_analysis(store.artifact(sid, "analysis.json").decode(), s, segments)
    files = {
        "protokoll-begruendet.md": render_analysis(result, s.speaker_aliases, segments).encode(),
        "protokoll-interpretiert.md": render_interpretation(
            result, s.speaker_aliases, segments
        ).encode(),
    }
    if sum(map(len, files.values())) > MAX_PROTOCOL_BYTES:
        raise ValueError("protocol exceeds configured attachment limit")
    if not callable(getattr(delivery, "send_files", None)):
        raise ValueError("attachment delivery is required for this protocol")
    if not store.begin_delivery(sid):
        return False
    try:
        mentions = {"parse": [], "replied_user": False}
        receipt = delivery.send_files(channel_id, files, allowed_mentions=mentions)
    except Exception:
        store.end_delivery(sid, None)
        raise
    store.end_delivery(sid, receipt)
    return bool(receipt)


def publish(
    store: Store, sid: str, channel_id: str, configured_channel_id: str, delivery: Delivery
) -> bool:
    store.session(sid)
    with store.files.lock(f"locks/{sid}.lock") as acquired:
        if not acquired:
            raise Conflict("session operation already running")
        return _publish(store, sid, channel_id, configured_channel_id, delivery)


class ProtocolPublisher:
    """Consume only committed LLM results; uncertain sends require manual reconciliation."""

    def __init__(self, store: Store, config: Config, delivery: Delivery):
        if store.tenant != config.tenant:
            raise ValueError("cross-guild protocol publisher")
        self.store, self.config, self.delivery = store, config, delivery

    def once(self) -> bool:
        with self.store.files.lock("locks/protocol-publisher.lock") as acquired:
            if not acquired:
                return False
            # Acquiring this single publisher lock proves an earlier sender is no longer
            # executing; its missing acknowledgement must never trigger an automatic resend.
            self.store.db.execute(
                """UPDATE protocol_outbox SET state=CASE WHEN EXISTS (
                    SELECT 1 FROM deliveries d WHERE d.session_id=protocol_outbox.session_id
                    AND d.state='sent') THEN 'sent' ELSE 'uncertain' END, error='sender_restart'
                   WHERE state='sending'"""
            )
            row = self.store.db.execute(
                "SELECT session_id,channel_id FROM protocol_outbox "
                "WHERE state='pending' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            sid, channel = row[0], row[1]
            self.store.protocol_state(sid, "sending")
            try:
                session = self.store.session(sid)
                if self.store.job_result(sid, "analysis") is None:
                    raise ValueError("committed LLM result required")
                current = self.config.protocol_channel(session.voice_channel_id)
                if current != channel or session.protocol_channel_id != channel:
                    raise PermissionError("protocol route changed or was disabled")
                sent = publish(self.store, sid, channel, current, self.delivery)
            except Conflict:
                # Commit and outbox enqueue share the broker's session lock. Seeing
                # that briefly occupied lock must not permanently block delivery.
                receipt = self.store.db.execute(
                    "SELECT state FROM deliveries WHERE session_id=?", (sid,)
                ).fetchone()
                state = (
                    "pending"
                    if receipt is None
                    else "sent"
                    if receipt[0] == "sent"
                    else "uncertain"
                )
                self.store.protocol_state(sid, state, "session_busy" if receipt is None else None)
            except Exception:
                receipt = self.store.db.execute(
                    "SELECT state FROM deliveries WHERE session_id=?", (sid,)
                ).fetchone()
                self.store.protocol_state(
                    sid,
                    "uncertain" if receipt else "blocked",
                    "delivery_unconfirmed" if receipt else "validation_failed",
                )
            else:
                receipt = self.store.db.execute(
                    "SELECT state FROM deliveries WHERE session_id=?", (sid,)
                ).fetchone()
                state = "sent" if sent or receipt and receipt[0] == "sent" else "uncertain"
                self.store.protocol_state(sid, state)
            return True
