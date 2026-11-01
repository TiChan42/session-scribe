"""Check public source/package inputs without printing matching private contents."""

from __future__ import annotations

import argparse
import re
import subprocess
import zipfile
from pathlib import Path

BLOCKED_PARTS = {".local", ".private", ".git", ".ssh", "secrets", ".venv", "__pycache__"}
BLOCKED_SUFFIXES = {".pem", ".key", ".sqlite", ".sqlite3", ".db", ".wav", ".flac", ".pcm", ".aiff"}
SECRET = re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----")


def check_name(name: str):
    path = Path(name)
    if (
        set(path.parts) & BLOCKED_PARTS
        or path.suffix in BLOCKED_SUFFIXES
        or path.name.startswith(".env")
        and not path.name.endswith(".example")
    ):
        raise ValueError("private/runtime path in public input")


def check_content(data: bytes):
    if SECRET.search(data):
        raise ValueError("private key material in public input")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    if args.wheel:
        with zipfile.ZipFile(args.wheel) as archive:
            for name in archive.namelist():
                check_name(name)
                check_content(archive.read(name))
        print("Wheel contains no forbidden private/runtime paths or private-key headers")
        return
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = Path(raw.decode())
        check_name(str(path))
        if path.is_symlink():
            raise ValueError("symlink in public source inputs")
        if path.is_file():
            check_content(path.read_bytes())
    ignore = Path(".dockerignore").read_text().splitlines()
    rules = [line for line in ignore if line and not line.startswith("#")]
    if not rules or rules[0] != "**":
        raise ValueError("container context must deny by default")
    expected = [
        "**",
        "!pyproject.toml",
        "!src/",
        "!src/sessionscribe/",
        "!src/sessionscribe/*.py",
        "!requirements/",
        "!requirements/core.txt",
        "!requirements/recorder.txt",
        "!requirements/stt.txt",
        "!requirements/voice-build.txt",
        "!requirements/voice-test.txt",
        "!scripts/",
        "!scripts/voice_candidate.py",
        "!containers/",
        "!containers/*.Dockerfile",
    ]
    if rules != expected:
        raise ValueError("review the changed container context allowlist")
    print("Public/staged source paths and explicit container context allowlist checked")


if __name__ == "__main__":
    main()
