from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sessionscribe.control import Controller
from sessionscribe.models import Actor, Config, Grant, InferenceSettings, Limits, Segment
from sessionscribe.recorder import Recorder
from sessionscribe.store import Store
from sessionscribe.transcript import save_transcript


@pytest.fixture
def rig(tmp_path):
    actions = ("start", "resume", "pause", "stop", "status", "process", "result", "delete")
    config = Config(
        mode="meeting",
        guild_id="1000",
        command_channel_ids=frozenset({"1002"}),
        voice_channel_ids=frozenset({"1003"}),
        output_channel_id="1004",
        default_protocol_channel_id="1004",
        grants={a: Grant(users=frozenset({"1001"}), roles=frozenset({"1010"})) for a in actions},
        inference=InferenceSettings(
            model="synthetic-fixture", context_tokens=32768, output_tokens=2048
        ),
        recording_enabled=True,
        cloud_enabled=True,
        limits=Limits(segment_ms=1000, min_free_bytes=0),
    )
    store = Store(tmp_path / "tenant", "1000")
    statuses = []
    holder = {}

    def status(channel, text):
        statuses.append((channel, text))
        return "synthetic-receipt"

    control = Controller(config, store, status, lambda _sid: holder["recorder"].close())
    actor = Actor(guild_id="1000", channel_id="1002", user_id="1001")
    other = actor.model_copy(update={"user_id": "1005"})
    session = control.start(
        actor,
        "1003",
        frozenset({actor.user_id, other.user_id}),
        speaker_aliases={actor.user_id: "Ada", other.user_id: "Ben"},
    )
    recorder = Recorder(store, session.id, config.limits)
    holder["recorder"] = recorder
    yield SimpleNamespace(
        config=config,
        store=store,
        control=control,
        actor=actor,
        other=other,
        session=session,
        recorder=recorder,
        statuses=statuses,
    )
    recorder.close()
    store.close()


def start(rig):
    for actor, alias in ((rig.actor, "Ada"), (rig.other, "Ben")):
        rig.control.alias_consent(actor, rig.session.id, alias, accepted=True)
        rig.control.consent(actor, rig.session.id, cloud=True)
    return rig.control.resume(rig.actor, rig.session.id)


@pytest.fixture
def transcript(rig):
    start(rig)
    rig.control.stop(rig.actor, rig.session.id)
    segment = Segment(
        id="a" * 32 + ":0000",
        session_id=rig.session.id,
        speaker_id=rig.actor.user_id,
        start_ms=100,
        end_ms=900,
        text="Kein Beschluss. Vorschlag: Ada prüft 12 Geräte bis Freitag. @everyone",
    )
    save_transcript(rig.store, rig.session.id, [segment])
    result = {
        "schema_version": 1,
        "tenant": "1000",
        "mode": "meeting",
        "session_id": rig.session.id,
        "findings": [
            {
                "kind": "proposal",
                "text": "Ada soll prüfen; kein Beschluss.",
                "rationale": "Die Aussage nennt ausdrücklich einen Vorschlag und verneint einen"
                " Beschluss.",
                "sources": [segment.id],
                "owner": None,
                "deadline": None,
                "category": None,
                "character": None,
            }
        ],
        "interpretation": {
            "summary": "Ein Vorschlag liegt vor; ein Beschluss fehlt.",
            "issues": [],
            "mermaid": None,
        },
        "justified_mermaid": None,
    }

    class Cloud:
        def __init__(self):
            self.calls = []
            self.result = result
            self.done_reason = "stop"
            self.tools = []

        def chat(self, request):
            self.calls.append(request)
            return {
                "done": True,
                "done_reason": self.done_reason,
                "message": {"content": json.dumps(self.result), "tool_calls": self.tools},
            }

    return SimpleNamespace(rig=rig, segment=segment, result=result, cloud=Cloud())
