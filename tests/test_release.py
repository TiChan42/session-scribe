"""Synthetic archive checks before CI signs and publishes deployable image bytes."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "image_release", Path(__file__).resolve().parents[1] / "scripts/release_image.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def archive(path, *, user="65532:65532", revision="a" * 40, env=None, extra=None, corrupt=False):
    config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {
            "User": user,
            "Entrypoint": ["python", "-m", "sessionscribe.cli"],
            "Cmd": ["bot", "--policy", "/config/bot.json"],
            "Labels": {"org.opencontainers.image.revision": revision},
            "Env": env or ["PATH=/usr/bin"],
        },
    }
    blobs = {}

    def blob(raw):
        digest = hashlib.sha256(raw).hexdigest()
        blobs["blobs/sha256/" + digest] = raw
        return {"digest": "sha256:" + digest, "size": len(raw)}

    manifest = blob(
        json.dumps(
            {"config": blob(json.dumps(config).encode()), "layers": [blob(b"synthetic-layer")]}
        ).encode()
    )
    files = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "index.json": json.dumps({"manifests": [manifest]}).encode(),
        **blobs,
        **(extra or {}),
    }
    if corrupt:
        files[next(name for name in blobs if blobs[name] == b"synthetic-layer")] = (
            b"changed-fixture"
        )
    with tarfile.open(path, "w") as output:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            output.addfile(member, io.BytesIO(raw))


def test_release_binds_archive_to_source_and_full_immutable_reference(tmp_path):
    file = tmp_path / "recorder.oci.tar"
    archive(file)
    result = release.inspect(file, "example/project", "a" * 40, "recorder")
    assert result["image"].startswith("ghcr.io/example/project-recorder@sha256:")
    assert result["revision"] == "a" * 40
    assert result["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "options",
    [
        {"user": "0:0"},
        {"revision": "b" * 40},
        {"env": ["DISCORD_TOKEN=synthetic-fixture"]},
        {"extra": {"../escape": b"synthetic"}},
        {"extra": {"blobs/sha256/" + "f" * 64: b"unreferenced"}},
        {"corrupt": True},
    ],
)
def test_unsafe_source_identity_content_or_archive_is_rejected(tmp_path, options):
    file = tmp_path / "recorder.oci.tar"
    archive(file, **options)
    with pytest.raises(ValueError):
        release.inspect(file, "example/project", "a" * 40, "recorder")
