from __future__ import annotations

import pytest

from sessionscribe.aliases import validate_alias
from sessionscribe.control import Denied
from sessionscribe.models import SessionState
from sessionscribe.store import Conflict


def test_aliases_are_normalized_bounded_and_safe_for_markdown():
    assert validate_alias("  Äda  ") == "Äda"
    assert validate_alias("Ａda") == "Ada"
    for value in ("", "  ", "x" * 33, "@everyone", "<@1001>", "a|b", "a\nb"):
        with pytest.raises(ValueError):
            validate_alias(value)


def test_waiting_session_requires_unique_aliases_and_each_session_consent(rig):
    # Aliases exist at session creation, but neither participant has consented yet.
    with pytest.raises(Denied, match="every participant"):
        rig.control.resume(rig.actor, rig.session.id)

    rig.control.alias_consent(rig.actor, rig.session.id, "Ada", accepted=True)
    with pytest.raises(Denied, match="every participant"):
        rig.control.resume(rig.actor, rig.session.id)

    with pytest.raises(Conflict, match="already used"):
        rig.control.alias_consent(rig.other, rig.session.id, "ada", accepted=True)

    rig.control.alias_consent(rig.other, rig.session.id, "Ben", accepted=True)
    assert rig.control.resume(rig.actor, rig.session.id).state == SessionState.RECORDING


def test_newcomer_pauses_audio_and_requires_alias_and_consent(rig):
    from sessionscribe.recorder import FRAME_BYTES, Frame

    rig.control.alias_consent(rig.actor, rig.session.id, "Ada", accepted=True)
    rig.control.alias_consent(rig.other, rig.session.id, "Ben", accepted=True)
    rig.control.resume(rig.actor, rig.session.id)
    assert rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))

    newcomer = "1006"
    updated = rig.control.membership(
        rig.session.id,
        rig.session.participants | {newcomer},
        speaker_aliases={newcomer: "Casey"},
    )
    assert updated.state == SessionState.PAUSED
    assert updated.speaker_aliases[newcomer] == "Casey"
    assert newcomer not in updated.consented
    assert not rig.recorder.accept(Frame(rig.actor.user_id, 20, bytes(FRAME_BYTES)))
    assert "Aufnahme pausiert" in rig.statuses[-1][1]

    first_reminder = updated.consent_reminder_at
    assert first_reminder is not None
    assert not rig.control.remind_consent(rig.session.id, first_reminder + 59)
    assert rig.control.remind_consent(rig.session.id, first_reminder + 60)
    assert "Alias(e)" in rig.statuses[-1][1]
    assert not rig.control.remind_consent(rig.session.id, first_reminder + 119)

    with pytest.raises(Denied, match="every participant"):
        rig.control.resume(rig.actor, rig.session.id)
    joined = rig.other.model_copy(update={"user_id": newcomer})
    rig.control.alias_consent(joined, rig.session.id, "Casey", accepted=True)
    assert rig.control.resume(rig.actor, rig.session.id).state == SessionState.RECORDING


def test_manual_pause_and_resume_are_explicit_and_close_the_write_gate(rig):
    from sessionscribe.recorder import FRAME_BYTES, Frame

    for participant, alias in ((rig.actor, "Ada"), (rig.other, "Ben")):
        rig.control.alias_consent(participant, rig.session.id, alias, accepted=True)
    rig.control.resume(rig.actor, rig.session.id)
    assert rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    paused = rig.control.pause(rig.actor, rig.session.id)
    assert paused.state == SessionState.PAUSED
    assert not rig.recorder.accept(Frame(rig.actor.user_id, 20, bytes(FRAME_BYTES)))
    assert rig.control.resume(rig.actor, rig.session.id).state == SessionState.RECORDING


def test_alias_rejection_pauses_and_revokes_recording_and_cloud_consent(rig):
    for participant, alias in ((rig.actor, "Ada"), (rig.other, "Ben")):
        rig.control.alias_consent(participant, rig.session.id, alias, accepted=True)
        rig.control.consent(participant, rig.session.id, cloud=True)
    rig.control.resume(rig.actor, rig.session.id)
    updated = rig.control.alias_consent(rig.other, rig.session.id, "Ben", accepted=False)
    assert updated.state == SessionState.PAUSED
    assert rig.other.user_id not in updated.consented
    assert rig.other.user_id not in updated.cloud_consent
    assert "abgelehnt" in rig.statuses[-1][1]
