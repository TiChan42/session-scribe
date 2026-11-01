"""Closed host supply chain: pin every wheel and reject private payloads before signing."""

import hashlib
import importlib.util
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
spec = importlib.util.spec_from_file_location(
    "host_bundle", Path(__file__).resolve().parents[1] / "scripts/host_bundle.py"
)
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def inputs(root, *, private=False):
    wheelhouse = root / "wheels"
    wheelhouse.mkdir()
    requirements = []
    for name in [
        "annotated_types",
        "pydantic",
        "pydantic_core",
        "typing_extensions",
        "typing_inspection",
    ]:
        file = wheelhouse / f"{name}-1.0-py3-none-any.whl"
        with zipfile.ZipFile(file, "w") as output:
            output.writestr(name + "/__init__.py", "# synthetic\n")
        requirements.append(f"{name}==1.0 --hash=sha256:{bundle.checksum(file)}\n")
    lock = root / "core.txt"
    lock.write_text("".join(requirements))
    application = root / "sessionscribe-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(application, "w") as output:
        output.writestr(
            ".local/operator.txt" if private else "sessionscribe/__init__.py", "synthetic"
        )
    return wheelhouse, application, lock


def test_closed_bundle_binds_only_reviewed_wheels_and_source_revision(tmp_path):
    wheels, application, requirements = inputs(tmp_path)
    output = tmp_path / "release"
    bundle.build("a" * 40, wheels, application, requirements, output)
    with tarfile.open(output / "host-core.tar") as archive:
        manifest = json.load(archive.extractfile("release.json"))
        assert manifest["revision"] == "a" * 40
        assert manifest["python_abi"] == "cp314"
        assert len(manifest["files"]) == 6
        assert len(archive.getnames()) == 9
    value = json.loads((output / "host-core.json").read_text())
    assert value["sha256"] == hashlib.sha256((output / "host-core.tar").read_bytes()).hexdigest()
    next(wheels.iterdir()).write_bytes(b"changed")
    with pytest.raises(ValueError):
        bundle.build("a" * 40, wheels, application, requirements, output)


def test_host_release_refuses_private_content_even_in_application_wheel(tmp_path):
    wheels, application, requirements = inputs(tmp_path, private=True)
    with pytest.raises(ValueError, match="private/runtime"):
        bundle.build("a" * 40, wheels, application, requirements, tmp_path / "release")
