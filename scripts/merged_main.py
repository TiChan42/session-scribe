"""Read-only CI admission: current main commit must be the merge commit of a merged PR."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request


def main():
    if os.environ["GITHUB_EVENT_NAME"] != "push" or os.environ["GITHUB_REF"] != "refs/heads/main":
        raise SystemExit("Only push-main builds are admitted")
    repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(
        r"[a-f0-9]{40}", sha
    ):
        raise SystemExit("Invalid build identity")

    def get(path):
        request = urllib.request.Request(
            "https://api.github.com/" + path,
            headers={
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    if get(f"repos/{repo}/git/ref/heads/main")["object"]["sha"] != sha:
        raise SystemExit("Superseded revision rejected")
    associated = get(f"repos/{repo}/commits/{sha}/pulls")
    if not any(
        p.get("merged_at")
        and p.get("merge_commit_sha") == sha
        and p["base"]["ref"] == "main"
        and p["base"]["repo"]["full_name"] == repo
        for p in associated
    ):
        raise SystemExit("Current revision is not a recognized merged PR; no build")
    if get(f"repos/{repo}/branches/main").get("protected") is not True:
        raise SystemExit("Main must be protected before building a release")
    deadline = time.monotonic() + 480
    while True:
        checks = get(f"repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100")
        selected = {
            check["name"]: check
            for check in checks.get("check_runs", [])
            if check.get("app", {}).get("slug") == "github-actions"
            and check.get("head_sha") == sha
            and check.get("name") in {"verify", "host-core"}
        }
        if any(
            check.get("status") == "completed" and check.get("conclusion") != "success"
            for check in selected.values()
        ):
            raise SystemExit("Required verification failed; no release")
        if set(selected) == {"verify", "host-core"} and all(
            check.get("conclusion") == "success" for check in selected.values()
        ):
            break
        if time.monotonic() >= deadline:
            raise SystemExit("Required verification did not complete; no release")
        time.sleep(10)
    if get(f"repos/{repo}/git/ref/heads/main")["object"]["sha"] != sha:
        raise SystemExit("Superseded revision rejected after verification")
    print("Protected current main, merged PR and both verification jobs confirmed")


if __name__ == "__main__":
    main()
