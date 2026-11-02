import hashlib
import json

import pytest

from sessionscribe.evaluator import run_handoff
from sessionscribe.files import SafeFiles


def test_evaluator_uses_separate_state_and_never_needs_recorder_database(transcript, tmp_path):
    t, rig = transcript, transcript.rig
    inputs = SafeFiles(tmp_path / "input")
    raw = rig.store.files.read(f"sessions/{rig.session.id}/transcript.json")
    inputs.atomic("transcript.json", raw)
    inputs.json(
        "handoff.json",
        {
            "schema_version": 1,
            "session": rig.store.session(rig.session.id).model_dump(mode="json"),
            "transcript_sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    state, output = tmp_path / "evaluator", tmp_path / "output"
    run_handoff(inputs.root, state, output, rig.config, t.cloud)
    assert (output / "result.md").exists()
    assert (output / "summary.md").exists()
    assert (state / "state.sqlite3").exists()
    assert rig.store.db.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 0
    assert len(t.cloud.calls) == 1
    run_handoff(inputs.root, state, output, rig.config, t.cloud)
    assert len(t.cloud.calls) == 1
    raw_handoff = json.loads(inputs.read("handoff.json"))
    raw_handoff["session"]["tenant"] = "2000"
    inputs.json("handoff.json", raw_handoff)
    with pytest.raises(ValueError, match="cross-tenant"):
        run_handoff(inputs.root, state, output, rig.config, t.cloud)
