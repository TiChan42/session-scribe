"""Real disk/queue integration with synthetic decoded packets; never log in to Discord."""

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from sessionscribe.bot import BotPolicy, ControlService
from sessionscribe.capture import RecordingBot
from sessionscribe.control import Denied
from sessionscribe.discord_receive import DiscordReceiver
from sessionscribe.guilds import AdminActor, GuildChannel, Setup
from sessionscribe.live import AudioIngress, receive_sink
from sessionscribe.models import InferenceSettings, Limits, SessionState
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.transcript import load_chunks
from sessionscribe.voice import ReceiveUnavailable


def receiver_case(case, monkeypatch):
    import discord

    session, live = resume(case)
    channel = Mock(spec=discord.VoiceChannel)
    channel.members = [SimpleNamespace(id=int(uid), bot=False) for uid in session.participants]
    voice = SimpleNamespace(
        _connection=SimpleNamespace(
            dave_session=SimpleNamespace(ready=True), dave_protocol_version=1
        ),
        start_recording=Mock(),
        is_recording=Mock(return_value=False),
        is_connected=Mock(return_value=True),
        disconnect=AsyncMock(),
    )
    channel.connect = AsyncMock(return_value=voice)
    client = SimpleNamespace(
        get_guild=lambda _gid: SimpleNamespace(get_channel=lambda _cid: channel)
    )

    async def call(fn, *args):
        return fn(*args)

    bot = SimpleNamespace(
        policy=case.policy,
        policy_call=call,
        service=case.service,
        send_status=AsyncMock(return_value="synthetic-visible-ack"),
    )
    receiver = DiscordReceiver(bot, case.ingress)
    receiver.clients = {"5001": client}
    module = types.ModuleType("discord.voice")
    module.VoiceClient = type("SyntheticVoiceClient", (), {})
    monkeypatch.setitem(sys.modules, "discord.voice", module)
    return receiver, session, live, channel, voice


@pytest.fixture
def live_case(tmp_path):
    policy = BotPolicy(
        data_root=tmp_path / "data",
        token_file=tmp_path / "synthetic.env",
        revision="development",
        inference=InferenceSettings(
            model="synthetic-model", context_tokens=16384, output_tokens=2048
        ),
        allowed_guild_ids=frozenset({"1000"}),
        recording_guild_ids=frozenset({"1000"}),
        processing_guild_ids=frozenset({"1000"}),
        cloud_guild_ids=frozenset({"1000"}),
        voice_reception="qualification",
        cloud_processing="native",
        limits=Limits(
            max_participants=4, max_duration_ms=300000, max_audio_bytes=1048576, min_free_bytes=0
        ),
    )
    ingress = AudioIngress(8)
    service = ControlService(
        policy, lambda *_: "visible-synthetic-receipt", Mock(), ingress=ingress
    )
    service.set_recording_bots((RecordingBot(id="5001", guild_ids=frozenset({"1000"})),))
    owner = AdminActor(
        guild_id="1000", channel_id="1002", user_id="1001", permissions=32, voice_channel_id="1003"
    )
    other = owner.model_copy(update={"user_id": "1005", "permissions": 0})
    service.configure(
        "setup",
        owner,
        Setup(
            mode="meeting",
            command_channel=GuildChannel(id="1002", guild_id="1000", kind="text"),
            voice_channel=GuildChannel(id="1003", guild_id="1000", kind="voice"),
            output_channel=GuildChannel(id="1004", guild_id="1000", kind="text"),
        ).model_dump(),
    )
    session = service.session(
        "startsession",
        owner,
        {"voice_channel_id": "1003", "participants": frozenset({"1001", "1005"})},
    )
    case = SimpleNamespace(
        policy=policy, service=service, ingress=ingress, owner=owner, other=other, session=session
    )
    yield case
    service.close()


def resume(case):
    for actor, alias in ((case.owner, "Ada"), (case.other, "Ben")):
        case.service.session(
            "alias",
            actor,
            {"alias": alias, "recording_consent": "accept"},
            current_participants=case.session.participants,
        )
        case.service.session(
            "consent", actor, {"session_id": case.session.id, "cloud_consent": True}
        )
    session = case.service.session(
        "resume",
        case.owner,
        {"session_id": case.session.id},
        current_participants=case.session.participants,
    )
    case.service.capture_ready("1000", session.id)
    live = case.ingress.activate(session, case.service.origins[("1000", session.id)])
    return session, live


def test_operator_scope_and_qualification_bounds_are_not_guild_permissions(live_case):
    case = live_case
    for change in (
        {"recording_guild_ids": frozenset({"2000"})},
        {"cloud_guild_ids": frozenset({"2000"})},
        {"limits": Limits(max_duration_ms=900000)},
    ):
        with pytest.raises(ValidationError):
            BotPolicy.model_validate(case.policy.model_dump() | change)
    with pytest.raises(Denied):
        case.service.configure("features", case.other, {"recording": True, "cloud_consent": True})
    with pytest.raises(Denied):
        case.service.session(
            "resume",
            case.owner,
            {"session_id": case.session.id},
            current_participants=case.session.participants,
        )


def test_two_decoded_speakers_reach_real_flac_and_durable_native_queue(live_case):
    case = live_case
    session, live = resume(case)
    state = SimpleNamespace(
        ssrc_user_map={10: 1001, 20: 1005}, dave_session=SimpleNamespace(ready=True)
    )
    sink = receive_sink(state, live, case.ingress)
    for ssrc in (10, 20):
        sink.write(
            SimpleNamespace(
                packet=SimpleNamespace(ssrc=ssrc, timestamp=960, decrypted_data=b"synthetic"),
                pcm=b"\x01\x00" * (FRAME_BYTES // 2),
            ),
            None,
        )
    case.service.poll_audio()
    stopped = case.service.session("stop", case.other, {"session_id": session.id})
    assert stopped.state == SessionState.STOPPED
    store = case.service.directory.store("1000")
    try:
        assert {c.speaker_id for c in load_chunks(store, session.id)} == {"1001", "1005"}
        assert len(store.queue_status()) == 1
        receipt = store.files.read(f"sessions/{session.id}/receive-receipt.json")
        assert b'"1001": 1' in receipt or b'"1001":1' in receipt
    finally:
        store.close()
    assert not case.service.pool.status()


def test_pause_invalidates_waiting_audio_and_resume_preserves_common_origin(live_case):
    case = live_case
    session, live = resume(case)
    origin = live.origin
    case.ingress.accept(live, Frame("1001", 0, bytes(FRAME_BYTES)))
    case.service.session("pause", case.owner, {"session_id": session.id})
    case.service.session(
        "resume", case.owner, {"session_id": session.id}, current_participants=session.participants
    )
    live = case.ingress.activate(session, origin)
    assert list(case.ingress.drain()) == []
    assert case.service.origins[("1000", session.id)] == origin


def test_unknown_source_interrupts_without_recording_its_audio(live_case):
    case = live_case
    session, live = resume(case)
    state = SimpleNamespace(ssrc_user_map={10: 9999}, dave_session=SimpleNamespace(ready=True))
    sink = receive_sink(state, live, case.ingress)
    sink.write(
        SimpleNamespace(
            packet=SimpleNamespace(ssrc=10, timestamp=0, decrypted_data=b"synthetic"),
            pcm=bytes(FRAME_BYTES),
        ),
        None,
    )
    assert not live.enabled.is_set()
    case.service.poll_audio()
    assert case.service.capture_session("1000", session.id).state == SessionState.INTERRUPTED
    assert case.service.capture_session("1000", session.id).stop_reason == "unapproved_voice_source"


def test_queue_overload_closes_write_gate_and_does_not_replay_after_resume(live_case):
    case = live_case
    session, live = resume(case)
    for i in range(9):
        case.ingress.accept(live, Frame("1001", i * 20, bytes(FRAME_BYTES)))
    assert live.failure == "audio_queue_full" and not live.enabled.is_set()
    case.service.poll_audio()
    assert case.service.capture_session("1000", session.id).stop_reason == "audio_queue_full"


def test_channel_loss_only_pauses_its_own_guild_channel(live_case):
    case = live_case
    session, _ = resume(case)
    assert case.service.pause_for_voice_state_loss("2000", "1003") == 0
    assert case.service.pause_for_voice_state_loss("1000", "9999") == 0
    assert case.service.capture_session("1000", session.id).state == SessionState.RECORDING
    assert case.service.pause_for_voice_state_loss("1000", "1003") == 1
    assert case.service.capture_session("1000", session.id).state == SessionState.PAUSED


def test_pinned_receiver_rejects_membership_change_during_handshake(live_case, monkeypatch):
    async def exercise():
        receiver, session, live, channel, voice = receiver_case(live_case, monkeypatch)

        async def connect(**_kwargs):
            live_case.ingress.pause(session.tenant, session.id)
            return voice

        channel.connect.side_effect = connect
        with pytest.raises(ReceiveUnavailable):
            await receiver.connect(session, live.origin)
        voice.start_recording.assert_not_called()
        assert not receiver.voices
        await receiver.close()

    asyncio.run(exercise())


def test_resumed_generation_can_receive_and_disconnect_does_not_block_other_audio(
    live_case, monkeypatch
):
    async def exercise():
        receiver, session, live, _channel, voice = receiver_case(live_case, monkeypatch)
        live_case.ingress.pause(session.tenant, session.id)
        await receiver.connect(session, live.origin)
        assert live.generation > 1
        voice.start_recording.assert_called_once()
        release = asyncio.Event()

        async def slow_close(**_kwargs):
            await release.wait()

        voice.disconnect.side_effect = slow_close
        await asyncio.wait_for(receiver.reconcile(set()), 0.1)
        assert receiver.disconnect_tasks
        release.set()
        await receiver.close()

    asyncio.run(exercise())


def test_shared_sdk_hooks_bind_tickets_to_explicit_sessions_and_restore_descriptors(
    live_case, monkeypatch
):
    import discord

    class WebSocket:
        def __init__(self, state):
            self.state = state

        @classmethod
        async def from_state(cls, state, **_kwargs):
            return cls(state)

        async def ready(self, data):
            return data

    original = WebSocket.__dict__["from_state"]
    for name, attributes in {
        "discord.voice.gateway": {"VoiceWebSocket": WebSocket},
        "discord.voice.receive.reader": {"UDPKeepAlive": SimpleNamespace(delay=5)},
    }.items():
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(discord, "__version__", "2.8.1+g10a5e8cf13bd8db21f95baf62cf866056d1e1091")

    async def exercise():
        receiver = DiscordReceiver(SimpleNamespace(policy=live_case.policy), live_case.ingress)
        issued = []

        async def request(payload, _expiry):
            if payload.get("transport") == "wss":
                issued.append(str(len(issued) + 1) * 64)
                return issued[-1]

        receiver.request = AsyncMock(side_effect=request)
        receiver.install()
        states = [SimpleNamespace(endpoint="synthetic.discord.media") for _ in range(2)]
        with pytest.raises(ReceiveUnavailable):
            await WebSocket.from_state(states[0])
        for index, state in enumerate(states):
            ctx = receiver.current_key.set(("1000", str(index) * 32))
            try:
                await WebSocket.from_state(state)
            finally:
                receiver.current_key.reset(ctx)
        assert receiver.tickets[id(states[0])] != receiver.tickets[id(states[1])]
        await receiver.disconnect(("1000", "0" * 32))
        assert id(states[0]) not in receiver.tickets and id(states[1]) in receiver.tickets
        await receiver.disconnect(("1000", "1" * 32))
        await receiver.close()
        assert WebSocket.__dict__["from_state"] is original

    asyncio.run(exercise())
