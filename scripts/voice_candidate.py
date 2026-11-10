"""Build the immutable MIT Pycord receiver; operator policy gates its activation."""

from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
import urllib.request
from pathlib import Path

REVISION = "10a5e8cf13bd8db21f95baf62cf866056d1e1091"
SHA256 = "d1e219134edf719df907c80146d4fe04f51b570a91653219c9a03235ee56a06f"
MAX_ARCHIVE = 16 * 1024**2


def prepare(archive, target):
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("bounded candidate archive required")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise ValueError("immutable candidate checksum mismatch")
    target.mkdir(mode=0o755, exist_ok=False)
    count = total = 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            parts = Path(member.name).parts
            if ".." in parts or Path(member.name).is_absolute():
                raise ValueError("unsafe candidate path")
            relative = Path(*parts[1:])
            allowed = str(relative) in {"LICENSE", "README.rst", "pyproject.toml", "setup.py"}
            allowed |= (
                len(relative.parts) > 1
                and relative.parts[0] in {"discord", "requirements"}
                and relative.suffix in {".py", ".txt"}
            )
            if not allowed or member.isdir():
                continue
            count += 1
            total += member.size
            if (
                not member.isfile()
                or count > 1000
                or member.size > 2 * 1024**2
                or total > MAX_ARCHIVE
            ):
                raise ValueError("bounded regular source files required")
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, path.open("xb") as output:
                output.write(stream.read(member.size + 1))
    license_text = (target / "LICENSE").read_text()
    if "The MIT License" not in license_text or "Pycord Development" not in license_text:
        raise ValueError("reviewed upstream license required")
    project = target / "pyproject.toml"
    original = project.read_text()
    patched, count = re.subn(
        r"(?ms)^\[tool\.setuptools\]\npackages = \[.*?^\]",
        '[tool.setuptools.packages.find]\ninclude = ["discord*"]',
        original,
    )
    if count != 1:
        raise ValueError("unexpected upstream package layout")
    project.write_text(patched)
    # The reviewed candidate still inherits the upstream 5000-second keepalive
    # bug. Use a five-second interruptible wait; never negotiate weaker crypto.
    reader = target / "discord/voice/receive/reader.py"
    before, marker, keepalive = reader.read_text().partition("class UDPKeepAlive(")
    if (
        not marker
        or keepalive.count("delay: int = 5000") != 1
        or keepalive.count("time.sleep(self.delay)") != 1
    ):
        raise ValueError("unexpected upstream keepalive implementation")
    keepalive = keepalive.replace("delay: int = 5000", "delay: int = 5")
    keepalive = keepalive.replace("time.sleep(self.delay)", "self._end_thread.wait(self.delay)")
    reader.write_text(before + marker + keepalive)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    archive = args.target.with_suffix(".tar.gz")
    request = urllib.request.Request(
        "https://api.github.com/repos/Pycord-Development/pycord/tarball/" + REVISION,
        headers={"User-Agent": "SessionScribe-reviewed-candidate"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("xb") as output:
        received = 0
        while chunk := response.read(1024**2):
            received += len(chunk)
            if received > MAX_ARCHIVE:
                raise ValueError("candidate download limit")
            output.write(chunk)
    prepare(archive, args.target)
