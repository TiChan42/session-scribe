"""A release needs protected current main, an actual merge and both matching CI checks."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_admission", Path(__file__).resolve().parents[1] / "scripts/merged_main.py"
)
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)


@pytest.mark.parametrize(
    "failure", [None, "unprotected", "not_merged", "check_failed", "superseded"]
)
def test_release_requires_the_whole_admission_chain(monkeypatch, failure):
    sha, repo = "a" * 40, "example/project"
    for key, value in {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": repo,
        "GITHUB_SHA": sha,
        "GITHUB_TOKEN": "synthetic-ci-fixture",
    }.items():
        monkeypatch.setenv(key, value)
    refs = 0

    def response(request, **_options):
        nonlocal refs
        url = request.full_url
        if url.endswith("/git/ref/heads/main"):
            refs += 1
            result = {"object": {"sha": "b" * 40 if failure == "superseded" and refs > 1 else sha}}
        elif url.endswith("/pulls"):
            result = (
                []
                if failure == "not_merged"
                else [
                    {
                        "merged_at": "synthetic-time",
                        "merge_commit_sha": sha,
                        "base": {"ref": "main", "repo": {"full_name": repo}},
                    }
                ]
            )
        elif url.endswith("/branches/main"):
            result = {"protected": failure != "unprotected"}
        elif "/check-runs?" in url:
            result = {
                "check_runs": [
                    {
                        "name": name,
                        "head_sha": sha,
                        "app": {"slug": "github-actions"},
                        "status": "completed",
                        "conclusion": "failure" if failure == "check_failed" else "success",
                    }
                    for name in ("verify", "host-core")
                ]
            }
        else:
            raise AssertionError("unexpected API request")
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(admission.urllib.request, "urlopen", response)
    if failure:
        with pytest.raises(SystemExit):
            admission.main()
    else:
        admission.main()
        assert refs == 2
