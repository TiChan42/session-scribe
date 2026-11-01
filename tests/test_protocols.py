import json

from conftest import start

from sessionscribe.broker import Broker, Workspace
from sessionscribe.demo import FixtureRunner
from sessionscribe.dispatch import Resources
from sessionscribe.handoff import digest
from sessionscribe.models import Analysis
from sessionscribe.pipeline import PipelineScheduler
from sessionscribe.recorder import FRAME_BYTES, Frame
from sessionscribe.render import ProtocolPublisher, render_analysis


class Delivery:
    def __init__(self):
        self.messages, self.files = [], []

    def send(self, channel_id, content, *, allowed_mentions):
        assert allowed_mentions["parse"] == []
        self.messages.append((channel_id, content))
        return "synthetic-message"

    def send_file(self, channel_id, filename, data, *, allowed_mentions):
        assert allowed_mentions["parse"] == []
        self.files.append((channel_id, filename, data))
        return "synthetic-attachment"


def pipeline(rig, tmp_path, *, long=False, cloud_error=False):
    start(rig)
    rig.recorder.accept(Frame(rig.actor.user_id, 0, bytes(FRAME_BYTES)))
    rig.control.stop(rig.actor, rig.session.id)
    runner = FixtureRunner(rig.config, tmp_path / "eval-state")
    original = runner.run

    def run(prepared):
        if prepared.binding.phase == "analysis" and cloud_error:
            raise OSError("synthetic cloud failure")
        original(prepared)
        if prepared.binding.phase == "analysis" and long:
            from sessionscribe.files import SafeFiles

            output = SafeFiles(prepared.output_path)
            result = json.loads(output.read("analysis.json"))
            result["findings"][0]["text"] = "Synthetischer Entwurf mit Quellen. " * 100
            output.json("analysis.json", result)
            output.atomic("result.md", render_analysis(Analysis.model_validate(result)).encode())
            done = json.loads(output.read("complete.json"))
            done["outputs"]["analysis.json"] = digest(output.read("analysis.json"))
            done["outputs"]["result.md"] = digest(output.read("result.md"))
            output.json("complete.json", done)

    runner.run = run
    for phase in ("stt", "analysis"):
        workspace = Workspace(
            "1000",
            phase,
            inbox=tmp_path / f"{phase}-in",
            outbox=tmp_path / f"{phase}-out",
            cache=tmp_path / f"{phase}-cache",
        )
        broker = Broker(
            rig.store,
            workspace,
            rig.config,
            runner,
            lambda: Resources(8 * 1024**3, 4 * 1024**3, 0, 4),
            tmp_path / "global.lock",
        )
        assert broker.once()


def test_only_committed_llm_protocol_is_enqueued_and_sent_once(rig, tmp_path):
    pipeline(rig, tmp_path)
    assert rig.store.protocol_queue()[0]["state"] == "pending"
    delivery = Delivery()
    publisher = ProtocolPublisher(rig.store, rig.config, delivery)
    assert publisher.once()
    assert rig.store.protocol_queue()[0]["state"] == "sent"
    assert not publisher.once()
    assert len(delivery.messages) == 1 and delivery.messages[0][0] == "1004"


def test_broker_session_lock_delays_delivery_without_permanently_blocking_it(rig, tmp_path):
    pipeline(rig, tmp_path)
    delivery = Delivery()
    publisher = ProtocolPublisher(rig.store, rig.config, delivery)
    with rig.store.files.lock(f"locks/{rig.session.id}.lock") as held:
        assert held and publisher.once()
        assert rig.store.protocol_queue()[0]["state"] == "pending"
        assert not delivery.messages
    assert publisher.once()
    assert rig.store.protocol_queue()[0]["state"] == "sent"
    assert len(delivery.messages) == 1


def test_conflict_after_network_send_is_uncertain_and_never_automatically_retried(rig, tmp_path):
    from sessionscribe.store import Conflict

    pipeline(rig, tmp_path)

    class Ambiguous(Delivery):
        def send(self, *args, **kwargs):
            super().send(*args, **kwargs)
            raise Conflict("synthetic receipt failure")

    delivery = Ambiguous()
    publisher = ProtocolPublisher(rig.store, rig.config, delivery)
    assert publisher.once()
    assert rig.store.protocol_queue()[0]["state"] == "uncertain"
    assert not publisher.once() and len(delivery.messages) == 1


def test_control_service_polls_committed_outbox_with_operator_guild_filter(rig, tmp_path):
    from sessionscribe.bot import BotPolicy, ControlService

    pipeline(rig, tmp_path)
    policy = BotPolicy(
        data_root=tmp_path / "control",
        token_file=tmp_path / "synthetic.env",
        revision="development",
        inference=rig.config.inference,
        allowed_guild_ids=frozenset({"2000"}),
    )
    service = ControlService(policy, lambda *_args: None, lambda *_args: None)
    delivery, destinations, closes = Delivery(), [], []

    class Borrowed:
        def __getattr__(self, name):
            return getattr(rig.store, name)

        def close(self):
            closes.append(True)

    def destination(guild):
        destinations.append(guild)
        return delivery

    try:
        service.directory.db.execute(
            "INSERT INTO configurations VALUES (?,1,?)", ("1000", rig.config.model_dump_json())
        )
        service.directory.store = lambda _guild: Borrowed()
        assert not service.publish_once(destination)
        assert not destinations
        service.policy = policy.model_copy(update={"allowed_guild_ids": frozenset({"1000"})})
        assert service.publish_once(destination)
        assert destinations == ["1000"] and len(delivery.messages) == 1
        assert not service.publish_once(destination)
        assert len(delivery.messages) == 1 and len(closes) == 2
    finally:
        service.close()


def test_long_protocol_uses_bounded_markdown_attachment(rig, tmp_path):
    pipeline(rig, tmp_path, long=True)
    delivery = Delivery()
    assert ProtocolPublisher(rig.store, rig.config, delivery).once()
    assert not delivery.messages and len(delivery.files) == 1
    channel, filename, data = delivery.files[0]
    assert channel == "1004" and filename == f"protocol-{rig.session.id}.md"
    assert b"Quellen" in data and len(data) > 1900


def test_cloud_failure_keeps_transcript_and_never_creates_publication(rig, tmp_path):
    pipeline(rig, tmp_path, cloud_error=True)
    assert rig.store.artifact(rig.session.id, "transcript.json")
    assert rig.store.protocol_queue() == []
    assert not ProtocolPublisher(rig.store, rig.config, Delivery()).once()


def test_no_configured_protocol_target_means_no_publication(rig, tmp_path):
    session = rig.store.session(rig.session.id)
    rig.store.save_session(session, session.model_copy(update={"protocol_channel_id": None}))
    rig.config = rig.config.model_copy(update={"default_protocol_channel_id": None})
    pipeline(rig, tmp_path)
    assert rig.store.job_result(rig.session.id, "analysis")
    assert rig.store.protocol_queue() == []


def test_route_change_does_not_silently_redirect_old_protocols(rig, tmp_path):
    pipeline(rig, tmp_path)
    delivery = Delivery()
    changed = rig.config.model_copy(update={"default_protocol_channel_id": "1009"})
    assert ProtocolPublisher(rig.store, changed, delivery).once()
    assert rig.store.protocol_queue()[0]["state"] == "blocked"
    assert not delivery.messages


def test_ambiguous_send_and_sender_crash_are_not_repeated(rig, tmp_path):
    pipeline(rig, tmp_path)

    class Ambiguous(Delivery):
        def send(self, *args, **kwargs):
            super().send(*args, **kwargs)
            raise OSError("synthetic lost acknowledgement")

    delivery = Ambiguous()
    worker = ProtocolPublisher(rig.store, rig.config, delivery)
    assert worker.once() and not worker.once()
    assert rig.store.protocol_queue()[0]["state"] == "uncertain"
    assert len(delivery.messages) == 1


def test_sender_restart_quarantines_inflight_outbox_item(rig, tmp_path):
    pipeline(rig, tmp_path)
    rig.store.protocol_state(rig.session.id, "sending")
    delivery = Delivery()
    assert not ProtocolPublisher(rig.store, rig.config, delivery).once()
    assert rig.store.protocol_queue()[0]["state"] == "uncertain"
    assert not delivery.messages


def test_round_robin_scheduler_does_not_starve_other_guild_queues():
    seen = []

    def consume(guild):
        seen.append(guild)
        return True

    scheduler = PipelineScheduler((lambda: consume("1000"), lambda: consume("2000")))
    assert scheduler.once() and scheduler.once() and scheduler.once()
    assert seen == ["1000", "2000", "1000"]
    assert not PipelineScheduler(()).once()


def test_restart_keeps_an_already_acknowledged_publication_sent(rig, tmp_path):
    pipeline(rig, tmp_path)
    delivery = Delivery()
    worker = ProtocolPublisher(rig.store, rig.config, delivery)
    assert worker.once()
    rig.store.protocol_state(
        rig.session.id, "sending"
    )  # Crash after durable receipt, before outbox update.
    assert not worker.once()
    assert rig.store.protocol_queue()[0]["state"] == "sent"
    assert len(delivery.messages) == 1
