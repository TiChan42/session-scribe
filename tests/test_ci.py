import importlib.util
import io
import json
from pathlib import Path

import pytest


def admission(monkeypatch, *, head="a" * 40, merged=True):
    for key, value in {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "example/sessionscribe",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_TOKEN": "synthetic-read-token",
    }.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("merged_main", Path("scripts/merged_main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def response(request, **_kwargs):
        if request.full_url.endswith("/git/ref/heads/main"):
            data = {"object": {"sha": head}}
        elif request.full_url.endswith("/branches/main"):
            data = {"protected": True}
        elif "/check-runs?" in request.full_url:
            data = {
                "check_runs": [
                    {
                        "name": name,
                        "head_sha": "a" * 40,
                        "app": {"slug": "github-actions"},
                        "status": "completed",
                        "conclusion": "success",
                    }
                    for name in ("verify", "host-core")
                ]
            }
        else:
            data = [
                {
                    "merged_at": "synthetic-date" if merged else None,
                    "merge_commit_sha": "a" * 40,
                    "base": {"ref": "main", "repo": {"full_name": "example/sessionscribe"}},
                }
            ]
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", response)
    return module


def test_ci_admits_only_current_merged_main(monkeypatch):
    admission(monkeypatch).main()
    with pytest.raises(SystemExit, match="Superseded"):
        admission(monkeypatch, head="b" * 40).main()
    with pytest.raises(SystemExit, match="recognized merged PR"):
        admission(monkeypatch, merged=False).main()
    module = admission(monkeypatch)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    with pytest.raises(SystemExit, match="Only push-main"):
        module.main()
