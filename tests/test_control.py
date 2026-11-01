import pytest
from conftest import start
from pydantic import ValidationError

from sessionscribe.commands import CommandRouter, definitions
from sessionscribe.control import Controller, Denied
from sessionscribe.models import Actor, SessionState
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.store import Conflict


@pytest.mark.parametrize(
    "field,value",
    [
        ("guild_id", "2000"),
        ("channel_id", "2002"),
        ("user_id", "2001"),
    ],
)
def test_acl_denies_wrong_numeric_identity(rig, field, value):
    with pytest.raises(Denied):
        rig.control.queue(rig.actor.model_copy(update={field: value}))


def test_role_name_is_not_identity(rig):
    with pytest.raises(ValidationError):
        Actor(guild_id="1000", channel_id="1002", user_id="1001", role_ids=frozenset({"admin"}))
    assert rig.control.queue(rig.other.model_copy(update={"role_ids": frozenset({"1010"})})) == []


def test_no_audio_before_all_consent_and_visible_status(rig):
    frame = Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES))
    assert not rig.recorder.accept(frame)
    rig.control.consent(rig.actor, rig.session.id, cloud=True)
    with pytest.raises(Denied):
        rig.control.resume(rig.actor, rig.session.id)
    rig.control.consent(rig.other, rig.session.id, cloud=False)
    rig.control.status = lambda *_: ""
    with pytest.raises(Denied):
        rig.control.resume(rig.actor, rig.session.id)
    assert not rig.recorder.accept(frame)


def test_new_participant_pauses_and_rejoin_needs_renewed_consent(rig):
    start(rig)
    assert rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    new = "1006"
    changed = rig.control.membership(rig.session.id, rig.session.participants | {new})
    assert changed.state == SessionState.PAUSED
    assert not rig.recorder.open
    assert not rig.recorder.accept(Frame(rig.actor.user_id, 20, bytes(FRAME_BYTES)))
    with pytest.raises(Denied):
        rig.control.resume(rig.actor, rig.session.id)
    rig.control.membership(rig.session.id, frozenset({rig.actor.user_id}))
    rig.control.membership(rig.session.id, rig.session.participants)
    assert rig.other.user_id not in rig.store.session(rig.session.id).consented


def test_stop_is_local_during_cloud_or_discord_outage(rig):
    start(rig)
    rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))

    def offline(*_):
        raise OSError("synthetic network failure")

    rig.control.status = offline
    stopped = rig.control.stop(rig.other, rig.session.id)
    assert stopped.state == SessionState.STOPPED
    assert not rig.recorder.open
    assert rig.recorder.chunk_count == 1


def test_actions_require_ownership(rig):
    other = rig.other.model_copy(update={"role_ids": frozenset({"1010"})})
    with pytest.raises(Denied):
        rig.control.delete(other, rig.session.id)
    with pytest.raises(Denied):
        rig.control.process(other, rig.session.id)


def test_pause_revoke_and_restart_never_auto_resume(rig):
    start(rig)
    rig.control.revoke(rig.other, rig.session.id)
    with pytest.raises(Denied):
        rig.control.resume(rig.actor, rig.session.id)
    assert rig.store.interrupt_recordings() == 1
    assert rig.store.session(rig.session.id).state == SessionState.INTERRUPTED
    with pytest.raises(Conflict):
        rig.control.resume(rig.actor, rig.session.id)
    assert rig.store.interrupt_recordings() == 0


def test_disabled_configuration_and_disallowed_channel(rig):
    rig.control.stop(rig.actor, rig.session.id)
    disabled = Controller(
        rig.config.model_copy(update={"recording_enabled": False}),
        rig.store,
        rig.control.status,
        lambda _sid: None,
    )
    with pytest.raises(Denied):
        disabled.start(rig.actor, "1003", rig.session.participants)
    with pytest.raises(Denied):
        rig.control.start(rig.actor, "9999", rig.session.participants)


def test_slash_router_checks_every_action(rig):
    assert {d["name"] for d in definitions()} >= {"stop", "delete", "process", "consent"}
    assert all(not d["dm_permission"] for d in definitions())
    with pytest.raises(Denied):
        CommandRouter(rig.control).dispatch("queue", rig.other)
    with pytest.raises(ValueError):
        CommandRouter(rig.control).dispatch("execute", rig.actor)


def test_status_reports_active_session_without_queue(rig):
    result = CommandRouter(rig.control).dispatch("status", rig.actor)
    assert result["sessions"][0]["state"] == "waiting_consent"
    assert result["queue"] == []


def test_departed_attendee_can_revoke_cloud_consent(rig):
    start(rig)
    rig.control.membership(rig.session.id, frozenset({rig.actor.user_id}))
    rig.control.revoke(rig.other, rig.session.id)
    assert rig.other.user_id not in rig.store.session(rig.session.id).cloud_consent
