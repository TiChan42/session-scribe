"""Core-only host compatibility: no recorder, Discord or STT dependencies required."""

import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sessionscribe.broker import Broker, Workspace
from sessionscribe.dispatch import Resources
from sessionscribe.guilds import AdminActor, GuildChannel, GuildDirectory, Setup
from sessionscribe.models import InferenceSettings, Session, SessionState
from sessionscribe.store import Store


def session():
    return Session(
        id="a" * 32,
        tenant="1000",
        mode="meeting",
        owner_id="1001",
        voice_channel_id="1003",
        state=SessionState.STOPPED,
        participants=frozenset({"1001"}),
        attendees=frozenset({"1001"}),
        inference=InferenceSettings(model="synthetic", context_tokens=8192, output_tokens=1024),
    )


def test_trusted_host_imports_never_load_voice_audio_or_inference_engines():
    for name in ("broker", "guilds", "pipeline", "files", "store", "handoff"):
        importlib.import_module("sessionscribe." + name)
    assert not set(sys.modules) & {
        "discord",
        "soundfile",
        "numpy",
        "faster_whisper",
        "ctranslate2",
        "onnxruntime",
        "torch",
    }


def test_store_queue_leases_backup_and_tenant_guard(tmp_path):
    store = Store(tmp_path / "guild", "1000")
    try:
        current = session()
        store.add_session(current)
        job_id = store.enqueue(current.id, "stt")
        job = store.claim("stt")
        assert job["id"] == job_id
        store.assert_job(job)
        store.finish(job)
        target = tmp_path / "backup.sqlite3"
        store.backup(target)
        backup = sqlite3.connect(target)
        try:
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert backup.execute("SELECT state FROM jobs").fetchone() == ("succeeded",)
        finally:
            backup.close()
    finally:
        store.close()
    with pytest.raises(ValueError):
        Store(tmp_path / "guild", "2000")


def test_guild_setup_restart_and_preclaim_resource_admission(tmp_path):
    inference = session().inference
    directory = GuildDirectory(tmp_path / "control", inference)
    try:
        actor = AdminActor(guild_id="1000", channel_id="1002", user_id="1001", permissions=32)
        config = directory.setup(
            actor,
            Setup(
                mode="meeting",
                command_channel=GuildChannel(id="1002", guild_id="1000", kind="text"),
                voice_channel=GuildChannel(id="1003", guild_id="1000", kind="voice"),
                output_channel=GuildChannel(id="1004", guild_id="1000", kind="text"),
            ),
        )
        store = directory.store("1000")
        try:
            workspace = Workspace(
                "1000",
                "stt",
                inbox=tmp_path / "inbox",
                outbox=tmp_path / "outbox",
                cache=tmp_path / "cache",
            )

            class Runner:
                def recover(self, phase):
                    return True

                def run(self, _prepared):
                    raise AssertionError("resource admission must precede launch")

            broker = Broker(
                store,
                workspace,
                config,
                Runner(),
                lambda: Resources(0, 0, 0, 4),
                tmp_path / "global-stt.lock",
            )
            assert not broker.once()
        finally:
            store.close()
    finally:
        directory.close()
    reopened = GuildDirectory(tmp_path / "control", inference)
    try:
        assert reopened.configuration("1000")[0].default_protocol_channel_id == "1004"
    finally:
        reopened.close()


def test_configuration_fingerprint_survives_different_process_hash_seeds():
    from sessionscribe.handoff import config_data, digest, encoded
    from sessionscribe.models import Config, Grant

    config = Config(
        mode="meeting",
        guild_id="1000",
        command_channel_ids=frozenset({"1002", "1006"}),
        voice_channel_ids=frozenset({"1003", "1007"}),
        output_channel_id="1004",
        recorder_bot_ids=frozenset({"5001", "5002"}),
        grants={
            "status": Grant(users=frozenset({"1001", "1005"}), roles=frozenset({"1010", "1020"}))
        },
        inference=session().inference,
    )
    script = (
        "import sys; from sessionscribe.models import Config; "
        "from sessionscribe.handoff import config_data,digest,encoded; "
        "print(digest(encoded(config_data(Config.model_validate_json(sys.stdin.read())))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=config.model_dump_json(),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env={"PYTHONHASHSEED": "7", "PYTHONPATH": str(Path("src").absolute())},
    )
    assert result.stdout.strip() == digest(encoded(config_data(config)))


def test_schema_four_upgrade_keeps_a_consistent_private_backup(tmp_path):
    root = tmp_path / "guild"
    original = Store(root, "1000")
    original.add_session(session())
    before = original.db.execute("SELECT body FROM sessions").fetchone()[0]
    original.db.execute("DROP TABLE analysis_plans")
    original.db.execute("DROP TABLE retired_jobs")
    original.db.execute("PRAGMA user_version=4")
    original.close()
    upgraded = Store(root, "1000")
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert upgraded.db.execute("SELECT body FROM sessions").fetchone()[0] == before
        backup_path = upgraded.migration_backup
        assert backup_path is not None and backup_path.stat().st_mode & 0o777 == 0o600
        backup = sqlite3.connect(backup_path)
        try:
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
            assert backup.execute("SELECT body FROM sessions").fetchone()[0] == before
        finally:
            backup.close()
    finally:
        upgraded.close()
