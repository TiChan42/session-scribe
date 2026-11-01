"""Build a closed, hash-checked Linux/Python 3.14 core bundle; never include a worktree."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path

from check_public import check_content, check_name


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pins(text):
    result = {}
    for line in text.replace("\\\n", " ").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)==([^\s]+)\s+(.+)", line)
        if not match:
            raise ValueError("closed exact-version hash requirements required")
        name, version, values = match.groups()
        hashes = re.findall(r"--hash=sha256:([a-f0-9]{64})", values)
        if not hashes or re.sub(r"--hash=sha256:[a-f0-9]{64}", "", values).strip():
            raise ValueError("only SHA256 wheel pins allowed")
        result[name.replace("_", "-").lower()] = (version, frozenset(hashes))
    if set(result) != {
        "annotated-types",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
        "typing-inspection",
    }:
        raise ValueError("unexpected trusted host dependency closure")
    return result


def build(revision, wheelhouse, application, requirements, output):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("immutable source revision required")
    locked = pins(requirements.read_text())
    wheels = sorted(wheelhouse.iterdir())
    found = set()
    for wheel in wheels:
        if wheel.is_symlink() or not wheel.is_file() or not wheel.name.endswith(".whl"):
            raise ValueError("wheel-only host inputs required")
        parts = wheel.name[:-4].split("-")
        name, version = parts[:2]
        name = name.replace("_", "-").lower()
        if (
            name not in locked
            or name in found
            or (version, checksum(wheel))
            not in {(locked[name][0], digest) for digest in locked[name][1]}
        ):
            raise ValueError("unreviewed or duplicate host wheel")
        if not (
            parts[-3:] == ["py3", "none", "any"]
            or parts[-3:-1] == ["cp314", "cp314"]
            and "x86_64" in parts[-1]
        ):
            raise ValueError("Linux x86_64/Python 3.14 wheel required")
        found.add(name)
    if found != set(locked) or not re.fullmatch(
        r"sessionscribe-[A-Za-z0-9.]+-py3-none-any\.whl", application.name
    ):
        raise ValueError("complete core and application wheel required")
    sources = {wheel.name: wheel for wheel in [*wheels, application]}
    for path in sources.values():
        if path.is_symlink() or path.stat().st_size > 32 * 1024**2:
            raise ValueError("bounded regular wheel required")
        with zipfile.ZipFile(path) as archive:
            total = 0
            for entry in archive.infolist():
                check_name(entry.filename)
                if (
                    entry.file_size > 16 * 1024**2
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise ValueError("unsafe wheel entry")
                total += entry.file_size
                if total > 64 * 1024**2:
                    raise ValueError("expanded wheel size limit")
                check_content(archive.read(entry))
    app_hash = checksum(application)
    version = application.name.split("-")[1]
    data = {
        "core.txt": requirements.read_bytes(),
        "application.txt": f"sessionscribe=={version} --hash=sha256:{app_hash}\n".encode(),
    }
    manifest = {
        "schema": 1,
        "revision": revision,
        "python_abi": "cp314",
        "application_wheel": application.name,
        "files": {name: checksum(path) for name, path in sources.items()},
        "requirements_sha256": hashlib.sha256(data["core.txt"]).hexdigest(),
        "application_lock_sha256": hashlib.sha256(data["application.txt"]).hexdigest(),
    }
    data["release.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "host-core.tar"
    with tarfile.open(target, "w") as archive:
        for name in sorted([*sources, *data]):
            raw = sources[name].read_bytes() if name in sources else data[name]
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(raw), 0o644
            archive.addfile(info, io.BytesIO(raw))
    (output / "host-core.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "revision": revision,
                "archive": target.name,
                "python_abi": "cp314",
                "sha256": checksum(target),
                "bytes": target.stat().st_size,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    print("Closed host core wheels, public contents and immutable bundle hashes checked")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--application", required=True, type=Path)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.revision, args.wheelhouse, args.application, args.requirements, args.output)
