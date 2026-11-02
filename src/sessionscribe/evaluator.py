"""Evaluator-only handoff: private budget/output state, no recorder DB or audio mount."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .analysis import Gateway, evaluate
from .files import SafeFiles
from .handoff import AnalysisHandoff, Completion, digest
from .models import Config
from .store import Store


def run_handoff(
    input_root: Path, state_root: Path, output_root: Path, config: Config, gateway: Gateway
):
    files, output = SafeFiles(input_root), SafeFiles(output_root)
    handoff = AnalysisHandoff.model_validate_json(files.read("handoff.json"))
    if handoff.session.tenant != config.tenant:
        raise ValueError("cross-tenant evaluation handoff")
    raw = files.read("transcript.json")
    if hashlib.sha256(raw).hexdigest() != handoff.transcript_sha256:
        raise ValueError("transcript integrity failure")
    store = Store(state_root, config.tenant)
    sid = handoff.session.id
    try:
        try:
            existing = store.session(sid)
        except KeyError:
            store.add_session(handoff.session)
        else:
            if existing != handoff.session:
                raise ValueError("changed evaluation session snapshot")
        fingerprint = f"sessions/{sid}/input-sha256.json"
        try:
            previous = store.files.read(fingerprint).decode()
        except FileNotFoundError:
            store.files.atomic(fingerprint, handoff.transcript_sha256.encode())
        else:
            if previous != handoff.transcript_sha256:
                raise ValueError("changed evaluation transcript snapshot")
        store.files.atomic(f"sessions/{sid}/transcript.json", raw)
        evaluate(
            store,
            sid,
            gateway,
            enabled=config.cloud_enabled,
            max_calls=config.daily_cloud_calls,
            max_tokens=config.daily_cloud_tokens,
        )
        for name in ("analysis.json", "result.md", "summary.md", "inference.json"):
            output.atomic(name, store.files.read(f"sessions/{sid}/{name}"))
        completion = Completion(
            schema_version=1,
            session_id=sid,
            binding=handoff.binding,
            outputs={
                name: digest(output.read(name))
                for name in ("analysis.json", "result.md", "summary.md", "inference.json")
            },
        )
        output.json("complete.json", completion.model_dump(mode="json"))
    finally:
        store.close()
