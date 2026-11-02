from __future__ import annotations

from types import SimpleNamespace

import pytest

from sessionscribe.capture import CaptureBudget, RecordingBot, RecordingPool
from sessionscribe.control import Controller, Denied
from sessionscribe.models import Actor, Config, Grant, InferenceSettings, Limits, SessionState
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.store import Conflict, Store
from sessionscribe.transcript import load_chunks


@pytest.fixture
def capture_cases(tmp_path):
    bots = (
        RecordingBot(id="5001", guild_ids=frozenset({"1000", "2000"})),
        RecordingBot(id="5002", guild_ids=frozenset({"1000", "2000"})),
    )
    pool = RecordingPool(
        bots, CaptureBudget(max_reserved_audio_bytes=4 * 1024**2, min_free_bytes=0)
    )
    cases = []
    for gid in ("1000", "2000"):
        actor = Actor(guild_id=gid, channel_id=gid + "2", user_id="1001")
        config = Config(
            mode="meeting",
            guild_id=gid,
            command_channel_ids=frozenset({gid + "2"}),
            voice_channel_ids=frozenset({gid + "3", gid + "6", gid + "8"}),
            output_channel_id=gid + "4",
            default_protocol_channel_id=gid + "4",
            voice_protocol_channels={gid + "6": gid + "7"},
            recorder_bot_ids=frozenset({"5001", "5002"}),
            grants={
                key: Grant(users=frozenset({"1001"}))
                for key in (
                    "start",
                    "resume",
                    "pause",
                    "stop",
                    "status",
                    "process",
                    "result",
                    "delete",
                )
            },
            recording_enabled=True,
            cloud_enabled=True,
            inference=InferenceSettings(
                model="synthetic-fixture", context_tokens=32768, output_tokens=2048
            ),
            limits=Limits(max_audio_bytes=1024**2, min_free_bytes=0, segment_ms=1000),
        )
        store = Store(tmp_path / gid, gid)
        control = Controller(
            config,
            store,
            lambda *_: "synthetic-status",
            lambda sid, gid=gid: pool.close_session(gid, sid),
        )
        cases.append(SimpleNamespace(actor=actor, config=config, store=store, control=control))
    yield pool, cases
    pool.interrupt_all()
    for case in cases:
        case.store.close()


def begin(pool, case, channel):
    s = pool.start(
        case.control,
        case.actor,
        channel,
        frozenset({case.actor.user_id}),
        speaker_aliases={case.actor.user_id: "Ada"},
    )
    case.control.alias_consent(case.actor, s.id, "Ada", accepted=True)
    case.control.consent(case.actor, s.id, cloud=True)
    return case.control.resume(case.actor, s.id)


def test_parallel_channels_in_same_guild_use_distinct_recording_bots_and_disk(capture_cases):
    pool, (case, _) = capture_cases
    first = begin(pool, case, "10003")
    second = begin(pool, case, "10006")
    assert first.recorder_bot_id != second.recorder_bot_id
    assert first.protocol_channel_id == "10004" and second.protocol_channel_id == "10007"
    for t in range(0, 200, 20):
        for s in (first, second):
            assert pool.accept(
                s.recorder_bot_id, "1000", s.id, Frame("1001", t, bytes(FRAME_BYTES))
            )
    case.control.stop(case.actor, first.id)
    assert case.store.session(second.id).state == SessionState.RECORDING
    assert pool.accept(
        second.recorder_bot_id, "1000", second.id, Frame("1001", 200, bytes(FRAME_BYTES))
    )
    case.control.stop(case.actor, second.id)
    assert len(case.store.queue_status()) == 2
    assert all(j["state"] == "pending" for j in case.store.queue_status())
    assert load_chunks(case.store, first.id)[0].end_ms == 200
    assert load_chunks(case.store, second.id)[0].end_ms == 220
    assert pool.status() == []


def test_one_recording_bot_can_be_used_concurrently_on_different_guilds(capture_cases):
    pool, (first, second) = capture_cases
    a = begin(pool, first, "10003")
    b = begin(pool, second, "20003")
    assert a.recorder_bot_id == b.recorder_bot_id == "5001"
    assert len(pool.status()) == 2


def test_bot_and_channel_capacity_are_enforced_before_moving_connections(capture_cases):
    pool, (case, _) = capture_cases
    begin(pool, case, "10003")
    with pytest.raises(Conflict):
        begin(pool, case, "10003")
    begin(pool, case, "10006")
    with pytest.raises(Conflict, match="no free recording bot"):
        begin(pool, case, "10008")
    assert len(pool.status()) == 2


def test_stop_frees_only_its_bot_slot_and_pause_keeps_reservation(capture_cases):
    pool, (case, _) = capture_cases
    a = begin(pool, case, "10003")
    b = begin(pool, case, "10006")
    case.control.pause(case.actor, a.id)
    assert len(pool.status()) == 2
    case.control.stop(case.actor, a.id)
    c = begin(pool, case, "10008")
    assert c.recorder_bot_id == a.recorder_bot_id
    assert case.store.session(b.id).state == SessionState.RECORDING


def test_frames_are_bound_to_bot_guild_and_session(capture_cases):
    pool, (case, _) = capture_cases
    s = begin(pool, case, "10003")
    for bot, guild in (("5002", "1000"), ("5001", "2000")):
        with pytest.raises(Denied):
            pool.accept(bot, guild, s.id, Frame("1001", 0, bytes(FRAME_BYTES)))


@pytest.mark.parametrize("resource", ["sessions", "tracks", "disk"])
def test_global_capture_admission_rejects_without_creating_a_second_session(
    capture_cases, monkeypatch, resource
):
    pool, (case, _) = capture_cases
    begin(pool, case, "10003")
    if resource == "sessions":
        pool.budget = pool.budget.model_copy(update={"max_sessions": 1})
    elif resource == "tracks":
        pool.budget = pool.budget.model_copy(update={"max_reserved_tracks": 8})
    else:
        import sessionscribe.capture as module

        monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(Conflict):
        begin(pool, case, "10006")
    assert case.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_capture_store_lifetime_is_independent_of_command_store(capture_cases):
    pool, (case, _) = capture_cases
    s = begin(pool, case, "10003")
    path = case.store.files.root
    case.store.close()
    assert pool.accept(s.recorder_bot_id, "1000", s.id, Frame("1001", 0, bytes(FRAME_BYTES)))
    # Restore the command connection for fixture teardown and orderly stop.
    case.store = Store(path, "1000")
    case.control.store = case.store
    case.control.stop(case.actor, s.id)
    assert len(case.store.queue_status()) == 1


def test_shutdown_finalizes_and_queues_each_session_without_auto_resume(capture_cases):
    pool, (case, _) = capture_cases
    sessions = [begin(pool, case, channel) for channel in ("10003", "10006")]
    for s in sessions:
        pool.accept(s.recorder_bot_id, "1000", s.id, Frame("1001", 0, bytes(FRAME_BYTES)))
    pool.interrupt_all()
    assert pool.status() == []
    assert all(case.store.session(s.id).state == SessionState.INTERRUPTED for s in sessions)
    assert len(case.store.queue_status()) == 2


def test_recorder_does_not_read_closed_audio_chunks_into_ram(rig, monkeypatch):
    from conftest import start

    start(rig)

    def forbidden_read(*_args, **_kwargs):
        pytest.fail("recorder reread a full audio segment")

    monkeypatch.setattr(rig.store.files, "read", forbidden_read)
    assert rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    rig.control.stop(rig.actor, rig.session.id)
    assert rig.recorder.chunk_count == 1
    assert len(rig.store.queue_status()) == 1


def test_capture_continues_while_another_session_runs_through_background_queue(
    capture_cases, tmp_path
):
    from sessionscribe.broker import Broker, Workspace
    from sessionscribe.demo import FixtureRunner
    from sessionscribe.dispatch import Resources
    from sessionscribe.models import SessionState
    from sessionscribe.pipeline import PipelineScheduler
    from sessionscribe.render import ProtocolPublisher

    pool, (case, _) = capture_cases
    first, second = [begin(pool, case, channel) for channel in ("10003", "10006")]
    for s in (first, second):
        pool.accept(s.recorder_bot_id, "1000", s.id, Frame("1001", 0, bytes(FRAME_BYTES)))
    case.control.stop(case.actor, first.id)
    runner = FixtureRunner(case.config, tmp_path / "pipeline-eval-state")
    original = runner.run
    timeline = iter((20, 40))

    def background(prepared):
        assert pool.accept(
            second.recorder_bot_id,
            "1000",
            second.id,
            Frame("1001", next(timeline), bytes(FRAME_BYTES)),
        )
        original(prepared)

    runner.run = background
    brokers = []
    for phase in ("stt", "analysis"):
        workspace = Workspace(
            "1000",
            phase,
            inbox=tmp_path / f"background-{phase}-in",
            outbox=tmp_path / f"background-{phase}-out",
            cache=tmp_path / f"background-{phase}-cache",
        )
        brokers.append(
            Broker(
                case.store,
                workspace,
                case.config,
                runner,
                lambda: Resources(8 * 1024**3, 4 * 1024**3, 0, 4),
                tmp_path / "background.lock",
            )
        )
    sent = []

    class Delivery:
        def send_files(self, *_args, **_kwargs):
            sent.append("10004")
            return "synthetic-publication"

        def send(self, channel_id, content, *, allowed_mentions):
            sent.append(channel_id)
            return "synthetic-publication"

    publisher = ProtocolPublisher(case.store, case.config, Delivery())
    scheduler = PipelineScheduler(tuple(b.once for b in brokers) + (publisher.once,))
    assert scheduler.once() and scheduler.once() and scheduler.once()
    assert sent == ["10004"]
    assert case.store.session(second.id).state == SessionState.RECORDING
    case.control.stop(case.actor, second.id)
    assert load_chunks(case.store, second.id)[0].end_ms == 60
