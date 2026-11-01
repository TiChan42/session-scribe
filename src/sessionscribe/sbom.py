"""Deterministic CycloneDX 1.6 inventory of installed Python packages (not OS/native libs)."""

from __future__ import annotations

import importlib.metadata
import json
import re
from urllib.parse import quote


def inventory() -> dict:
    components = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        purl = f"pkg:pypi/{quote(normalized)}@{quote(dist.version)}"
        component = {
            "type": "library",
            "name": normalized,
            "version": dist.version,
            "purl": purl,
            "bom-ref": purl,
        }
        # Existing package metadata only, never invent a license from a repository name.
        if expression := dist.metadata.get("License-Expression"):
            component["licenses"] = [{"expression": expression}]
        components.append(component)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": sorted(components, key=lambda c: (c["name"], c["version"])),
    }


if __name__ == "__main__":
    print(json.dumps(inventory(), sort_keys=True, indent=2))
