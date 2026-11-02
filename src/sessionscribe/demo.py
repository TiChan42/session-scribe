"""Entirely synthetic broker pipeline. No Discord, cloud or host commands are executed."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from .broker import Broker, Prepared, Workspace
from .control import Controller
from .dispatch import Resources
from .evaluator import run_handoff as evaluate_handoff
from .files import SafeFiles
from .handoff import Checkpoint, Completion, SttHandoff, digest
from .model_cache import REVISION
from .models import Actor, Config, Grant, InferenceSettings, Limits
from .recorder import FRAME_BYTES, Frame, Recorder
from .render import ProtocolPublisher
from .store import Store
from .stt import transcribe_chunk
from .transcript import canonicalize, load_transcript


def synthetic_config(mode: str) -> Config:
    grants = {
        a: Grant(users=frozenset({"1001"}))
        for a in ("start", "resume", "pause", "stop", "status", "process", "result", "delete")
    }
    return Config(
        mode=mode,
        guild_id="1000" if mode == "meeting" else "2000",
        command_channel_ids=frozenset({"1002"}),
        voice_channel_ids=frozenset({"1003"}),
        output_channel_id="1004",
        default_protocol_channel_id="1004",
        grants=grants,
        inference=InferenceSettings(
            model="synthetic-fixture", context_tokens=32768, output_tokens=2048
        ),
        recording_enabled=True,
        cloud_enabled=True,
        limits=Limits(segment_ms=1000, min_free_bytes=0),
    )


class FixtureRunner:
    """Local demo implementation of the supervisor interface. Never launches a process."""

    def __init__(self, config: Config, evaluator_state: Path):
        self.config, self.evaluator_state = config, evaluator_state

    def recover(self, _phase) -> bool:
        return True  # No background processes exist in this synchronous fixture.

    def quiesce(self, _prepared) -> bool:
        return True

    def run(self, prepared: Prepared):
        if prepared.binding.phase == "stt":
            inputs, outputs = SafeFiles(prepared.input_path), SafeFiles(prepared.output_path)
            handoff = SttHandoff.model_validate_json(inputs.read("handoff.json"))
            segments = []
            for chunk in handoff.chunks:

                class Model:
                    def transcribe(self, _audio, chunk=chunk, **_kwargs):
                        return iter(
                            [
                                SimpleNamespace(
                                    start=0,
                                    end=(chunk.end_ms - chunk.start_ms) / 1000,
                                    text="Synthetische Probe: kein Beschluss. Termin unbekannt.",
                                )
                            ]
                        ), None

                current = transcribe_chunk(Model(), inputs, handoff.session_id, chunk)
                segments.extend(current)
                outputs.json(
                    f"chunks/{chunk.id}.json",
                    Checkpoint(audio_sha256=chunk.sha256, segments=current).model_dump(mode="json"),
                )
            outputs.json(
                "transcript.json",
                [s.model_dump(mode="json") for s in canonicalize(segments, handoff.session_id)],
            )
            complete = Completion(
                schema_version=1,
                session_id=handoff.session_id,
                binding=handoff.binding,
                model_revision=REVISION,
                compute_type="int8",
                outputs={"transcript.json": digest(outputs.read("transcript.json"))},
            )
            outputs.json("complete.json", complete.model_dump(mode="json"))
        else:

            class Cloud:
                def chat(self, request):
                    assert "tools" not in request
                    data = json.loads(request["messages"][1]["content"])
                    result = {
                        "schema_version": 1,
                        "tenant": data["tenant"],
                        "mode": data["mode"],
                        "session_id": data["session_id"],
                        "findings": [
                            {
                                "kind": "summary",
                                "text": "Synthetische Probe: kein Beschluss.",
                                "rationale": "Die Quelle verneint ausdrücklich einen Beschluss.",
                                "sources": [s["id"] for s in data["transcript"]],
                                "owner": None,
                                "deadline": None,
                                "character": None,
                                "category": "unclear" if data["mode"] == "dnd" else None,
                            }
                        ],
                        "interpretation": {
                            "summary": "Es wurde kein Beschluss gefasst.",
                            "issues": [],
                            "mermaid": None,
                        },
                        "justified_mermaid": None,
                    }
                    return {
                        "done": True,
                        "done_reason": "stop",
                        "message": {"content": json.dumps(result)},
                    }

            evaluate_handoff(
                prepared.input_path,
                self.evaluator_state,
                prepared.output_path,
                self.config,
                Cloud(),
            )


def demo(root: Path, mode: str) -> dict:
    if root.exists():
        raise ValueError("demo requires a fresh directory")
    config = synthetic_config(mode)
    store = Store(root / "recorder", config.tenant)
    holder = {}
    control = Controller(
        config,
        store,
        lambda _channel, _text: "synthetic-status",
        lambda _sid: holder["recorder"].close(),
    )
    actor = Actor(guild_id=config.guild_id, channel_id="1002", user_id="1001")
    other = actor.model_copy(update={"user_id": "1005"})
    session = control.start(actor, "1003", frozenset({actor.user_id, other.user_id}))
    recorder = Recorder(store, session.id, config.limits)
    holder["recorder"] = recorder
    try:
        for person in (actor, other):
            control.consent(person, session.id, cloud=True)
        control.resume(actor, session.id)
        for timestamp in range(0, 600, 20):
            recorder.accept(Frame(actor.user_id, timestamp, bytes(FRAME_BYTES)))
            if timestamp >= 200:
                recorder.accept(Frame(other.user_id, timestamp, bytes(FRAME_BYTES)))
        recorder.accept(Frame(actor.user_id, 1000, bytes(FRAME_BYTES)))
        control.stop(other, session.id)
        control.process(actor, session.id)
        runner = FixtureRunner(config, root / "evaluator-state")
        for phase in ("stt", "analysis"):
            workspace = Workspace(
                config.tenant,
                phase,
                inbox=root / f"{phase}-inputs",
                outbox=root / f"{phase}-outputs",
                cache=root / f"{phase}-cache",
            )
            broker = Broker(
                store,
                workspace,
                config,
                runner,
                lambda: Resources(8 * 1024**3, 4 * 1024**3, 0, 4),
                root.parent / "synthetic-stt.lock",
            )
            if not broker.once():
                raise RuntimeError("synthetic broker did not consume its job")

        class FixtureDelivery:
            def send(self, _channel, _content, *, allowed_mentions):
                assert allowed_mentions["parse"] == []
                return "synthetic-protocol-receipt"

            def send_file(self, _channel, _filename, _data, *, allowed_mentions):
                assert allowed_mentions["parse"] == []
                return "synthetic-attachment-receipt"

        if not ProtocolPublisher(store, config, FixtureDelivery()).once():
            raise RuntimeError("synthetic protocol outbox was not consumed")
        segments = load_transcript(store, session.id)
        completed = Completion.model_validate(store.job_result(session.id, "analysis"))
        return {
            "mode": "synthetic broker pipeline (STT/cloud/supervisor fixtures)",
            "tenant": config.tenant,
            "profile": config.mode,
            "session_id": session.id,
            "audio_chunks": recorder.chunk_count,
            "transcript_segments": len(segments),
            "queue": store.queue_status(),
            "protocol_outbox": store.protocol_queue(),
            "result": str(root / "recorder" / "results" / completed.binding.attempt / "result.md"),
        }
    finally:
        recorder.close()
        store.close()
