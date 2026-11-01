"""Operator-only model preparation. Never run by the recorder or offline worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from sessionscribe.model_cache import REPOSITORY, REVISION, SHA256

FILES = tuple(SHA256)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    target = parser.parse_args().destination
    target.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in FILES:
        part = target / (name + ".part")
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as response, part.open("xb") as output:
            total = 0
            while data := response.read(1024 * 1024):
                total += len(data)
                if total > 600_000_000:
                    raise ValueError("model file exceeds preparation budget")
                output.write(data)
                digest.update(data)
        if digest.hexdigest() != SHA256[name]:
            raise ValueError("model download integrity failure")
        part.rename(target / name)
        hashes[name] = digest.hexdigest()
    (target / "provenance.json").write_text(
        json.dumps(
            {"repository": REPOSITORY, "revision": REVISION, "sha256": hashes, "license": "MIT"},
            indent=2,
        )
        + "\n"
    )
    print("Pinned multilingual small model downloaded; read-only mount required for workers.")


if __name__ == "__main__":
    main()
