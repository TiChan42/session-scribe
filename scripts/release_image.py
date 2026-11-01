"""Check a credential-free OCI archive and bind it to reviewed source before signing."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path

DIGEST = re.compile(r"sha256:([a-f0-9]{64})\Z")
MAX_ARCHIVE = 2 * 1024**3


def inspect(path: Path, repository: str, revision: str, stage: str):
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[a-f0-9]{40}", revision)
        or stage not in {"recorder", "stt", "evaluator"}
        or path.is_symlink()
        or not path.is_file()
        or not 1 <= path.stat().st_size <= MAX_ARCHIVE
    ):
        raise ValueError("invalid immutable image artifact")
    with tarfile.open(path, mode="r:") as archive:
        files = {}
        for index, member in enumerate(archive):
            if index >= 256:
                raise ValueError("OCI archive member limit")
            name = member.name.removeprefix("./")
            if member.isdir() and name.rstrip("/") in {".", "blobs", "blobs/sha256"}:
                continue
            if (
                not member.isfile()
                or name in files
                or not re.fullmatch(r"(?:index\.json|oci-layout|blobs/sha256/[a-f0-9]{64})", name)
                or not 0 < member.size <= MAX_ARCHIVE
            ):
                raise ValueError("unexpected OCI archive member")
            files[name] = member
        if not {"index.json", "oci-layout"} <= files.keys():
            raise ValueError("OCI layout missing")

        def read(name, limit):
            if name not in files or files[name].size > limit:
                raise ValueError("bounded OCI metadata required")
            with archive.extractfile(files[name]) as stream:
                return json.load(stream)

        layout = read("oci-layout", 4096)
        index = read("index.json", 65536)
        if layout != {"imageLayoutVersion": "1.0.0"} or len(index.get("manifests", [])) != 1:
            raise ValueError("exactly one OCI image required")
        digest = index["manifests"][0].get("digest", "")
        if not DIGEST.fullmatch(digest):
            raise ValueError("OCI manifest digest required")
        manifest = read("blobs/sha256/" + digest[7:], 262144)
        image_id = manifest.get("config", {}).get("digest", "")
        if not DIGEST.fullmatch(image_id):
            raise ValueError("OCI configuration digest required")
        config = read("blobs/sha256/" + image_id[7:], 131072)
        settings = config.get("config", {})
        expected_cmd = (
            ["bot", "--policy", "/config/bot.json"]
            if stage == "recorder"
            else ["stt-worker" if stage == "stt" else "evaluate-worker"]
        )
        if (
            config.get("os") != "linux"
            or config.get("architecture") != "amd64"
            or settings.get("User") != "65532:65532"
            or settings.get("Entrypoint") != ["python", "-m", "sessionscribe.cli"]
            or settings.get("Cmd") != expected_cmd
            or settings.get("Labels", {}).get("org.opencontainers.image.revision") != revision
            or any(
                re.search(r"(?:TOKEN|PASSWORD|SECRET|PRIVATE_KEY|AUTH)", key.split("=", 1)[0])
                for key in settings.get("Env", [])
            )
        ):
            raise ValueError("image source, user, command or environment rejected")
        layers = manifest.get("layers", [])
        if not 1 <= len(layers) <= 32:
            raise ValueError("bounded OCI layer list required")
        required = {"index.json", "oci-layout", "blobs/sha256/" + digest[7:]}
        for item in [manifest["config"], *layers]:
            value = item.get("digest", "")
            if not DIGEST.fullmatch(value):
                raise ValueError("SHA256 OCI blob required")
            name = "blobs/sha256/" + value[7:]
            if name not in files or item.get("size") != files[name].size:
                raise ValueError("OCI blob size mismatch")
            required.add(name)
        if set(files) != required:
            raise ValueError("unexpected extra OCI content")
        for name, member in files.items():
            if name.startswith("blobs/"):
                with archive.extractfile(member) as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != name.rsplit("/", 1)[1]:
                        raise ValueError("OCI blob digest mismatch")
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "schema": 1,
        "repository": repository,
        "revision": revision,
        "stage": stage,
        "image": f"ghcr.io/{repository.lower()}-{stage}@{digest}",
        "image_id": image_id,
        "archive": path.name,
        "sha256": checksum,
        "bytes": path.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    value = inspect(args.archive, args.repository, args.revision, args.stage)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print("OCI source, non-root entrypoint, bounded archive and blob hashes verified")


if __name__ == "__main__":
    main()
