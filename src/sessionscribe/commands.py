"""Slash command definitions/router, shared by the Discord transport and tests.

No gateway connection or command registration is performed by importing this module.
Transport must derive Actor and current voice membership from Discord events, never
from user-supplied display names, command options or model responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .control import Controller
from .models import Actor

if TYPE_CHECKING:
    from .capture import RecordingPool

BASE_COMMANDS = (
    "start",
    "alias",
    "alias-delete",
    "consent",
    "revoke",
    "pause",
    "resume",
    "stop",
    "status",
    "queue",
    "process",
    "result",
    "delete",
)
COMMAND_ALIASES = {
    "startsession": "start",
    "stopsession": "stop",
    "pausesession": "pause",
}
COMMANDS = (*BASE_COMMANDS, *COMMAND_ALIASES)


def canonical_command(name: str) -> str:
    return COMMAND_ALIASES.get(name, name)


def definitions() -> list[dict]:
    commands = [
        {"name": name, "description": f"SessionScribe: {name}", "type": 1, "dm_permission": False}
        for name in COMMANDS
    ]
    for command in commands:
        name = command["name"]
        behavior = canonical_command(name)
        if behavior == "start":
            command["options"] = [
                {
                    "name": "voice_channel",
                    "description": "Freigegebener Sprachkanal",
                    "type": 7,
                    "channel_types": [2],
                    "required": True,
                }
            ]
        elif behavior == "alias":
            command["options"] = [
                {
                    "name": "recording_consent",
                    "description": "Bei aktiver Sitzung ausdrücklich zustimmen oder ablehnen",
                    "type": 3,
                    "required": False,
                    "choices": [
                        {"name": "Ich stimme zu", "value": "accept"},
                        {"name": "Ich stimme nicht zu", "value": "decline"},
                    ],
                },
                {
                    "name": "alias",
                    "description": (
                        "Dein Name im Protokoll; bei aktiver Sitzung nur im Start-Befehlschat"
                    ),
                    "type": 3,
                    "required": False,
                    "min_length": 1,
                    "max_length": 32,
                },
            ]
        elif behavior == "alias-delete":
            continue
        elif behavior not in ("queue", "status"):
            command["options"] = [
                {
                    "name": "session_id",
                    "description": "Sitzungs-ID aus /status",
                    "type": 3,
                    "required": True,
                    "min_length": 32,
                    "max_length": 32,
                }
            ]
            if behavior == "consent":
                command["options"].append(
                    {
                        "name": "cloud_consent",
                        "description": "Textauswertung über Ollama Cloud ausdrücklich erlauben",
                        "type": 5,
                        "required": True,
                    }
                )
        if behavior == "process":
            command.setdefault("options", []).append(
                {
                    "name": "retry_analysis",
                    "description": "Gescheiterte Cloud-Auswertung mit neuem Plan wiederholen",
                    "type": 5,
                    "required": False,
                }
            )
        if behavior == "result":
            command["options"].append(
                {
                    "name": "raw_transcript",
                    "description": "Nur das geprüfte Rohtranskript abrufen",
                    "type": 5,
                    "required": False,
                }
            )
    return commands


class CommandRouter:
    def __init__(self, controller: Controller, recordings: RecordingPool | None = None):
        self.control, self.recordings = controller, recordings

    def dispatch(
        self,
        name: str,
        actor: Actor,
        *,
        session_id: str | None = None,
        voice_channel_id: str | None = None,
        participants: frozenset[str] = frozenset(),
        speaker_aliases: dict[str, str] | None = None,
        cloud_consent: bool = False,
        retry_analysis: bool = False,
        raw_transcript: bool = False,
    ):
        if name not in COMMANDS:
            raise ValueError("unknown command")
        name = canonical_command(name)
        if name == "start":
            if self.recordings is not None:
                return self.recordings.start(
                    self.control,
                    actor,
                    voice_channel_id,
                    participants,
                    speaker_aliases=speaker_aliases,
                )
            return self.control.start(
                actor,
                voice_channel_id,
                participants,
                speaker_aliases=speaker_aliases,
            )
        if name == "queue":
            return self.control.queue(actor)
        if name == "status":
            return self.control.status_for(actor)
        if name in ("alias", "alias-delete"):
            raise ValueError("self-bound aliases require the authenticated Discord control service")
        if session_id is None:
            raise ValueError("session ID required")
        if name == "consent":
            return self.control.consent(actor, session_id, cloud=cloud_consent)
        if name == "process" and retry_analysis:
            return self.control.restart_analysis(actor, session_id, self.control.config.inference)
        if name == "result" and raw_transcript:
            return self.control.transcript(actor, session_id)
        method = getattr(self.control, name)
        return method(actor, session_id)
