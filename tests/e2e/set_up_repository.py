"""Idempotently prepare the end-to-end test repository; run as an admin of it, logged in with `gh`."""

import base64
import os
import subprocess
from typing import Any

import requests
from actors import BRANCH_WITHOUT_MERGE_QUEUE, Actor

RULESET_NAME = "merge-queue"
REQUIRED_CHECK = "check"
# The GitHub Actions app, so the required check cannot be satisfied by a commit status from anyone else.
GITHUB_ACTIONS_APP_ID = 15368

WORKFLOWS = {
    ".github/workflows/check.yml": f"""\
name: check
on:
  pull_request:
  merge_group:
permissions: {{}}
jobs:
  {REQUIRED_CHECK}:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
""",
    ".github/workflows/bot-comment.yml": """\
name: bot-comment
on:
  workflow_dispatch:
    inputs:
      pr:
        required: true
      body:
        required: true
permissions:
  issues: write
  pull-requests: write
jobs:
  comment:
    runs-on: ubuntu-latest
    steps:
      - run: gh pr comment "$PR" --repo "$GITHUB_REPOSITORY" --body "$BODY"
        env:
          GH_TOKEN: ${{ github.token }}
          PR: ${{ inputs.pr }}
          BODY: ${{ inputs.body }}
""",
}

RULESET = {
    "name": RULESET_NAME,
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    # Org admins may still update the workflows above on reruns of this script.
    "bypass_actors": [
        {"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}
    ],
    "rules": [
        {
            "type": "merge_queue",
            "parameters": {
                "check_response_timeout_minutes": 10,
                "grouping_strategy": "ALLGREEN",
                "max_entries_to_build": 5,
                "max_entries_to_merge": 5,
                "merge_method": "SQUASH",
                "min_entries_to_merge": 1,
                "min_entries_to_merge_wait_minutes": 0,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [
                    {
                        "context": REQUIRED_CHECK,
                        "integration_id": GITHUB_ACTIONS_APP_ID,
                    }
                ],
            },
        },
    ],
}


def fetch_if_present(admin: Actor, path: str) -> Any | None:
    try:
        return admin.call_api("GET", path)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            return None
        raise


def ensure_workflows(admin: Actor, default_branch: str) -> None:
    for path, content in WORKFLOWS.items():
        existing = fetch_if_present(
            admin, f"/repos/{admin.repo}/contents/{path}?ref={default_branch}"
        )
        if existing and base64.b64decode(existing["content"]).decode() == content:
            print(f"{path}: up to date")
            continue
        admin.call_api(
            "PUT",
            f"/repos/{admin.repo}/contents/{path}",
            {
                "message": f"chore: update {path}",
                "content": base64.b64encode(content.encode()).decode(),
                "branch": default_branch,
            }
            | ({"sha": existing["sha"]} if existing else {}),
        )
        print(f"{path}: written")


def ensure_branch_without_merge_queue(admin: Actor, default_branch: str) -> None:
    branch = f"/repos/{admin.repo}/git/ref/heads/{BRANCH_WITHOUT_MERGE_QUEUE}"
    if fetch_if_present(admin, branch):
        print(f"branch {BRANCH_WITHOUT_MERGE_QUEUE}: exists")
        return
    head = admin.call_api("GET", f"/repos/{admin.repo}/git/ref/heads/{default_branch}")
    admin.call_api(
        "POST",
        f"/repos/{admin.repo}/git/refs",
        {
            "ref": f"refs/heads/{BRANCH_WITHOUT_MERGE_QUEUE}",
            "sha": head["object"]["sha"],
        },
    )
    print(f"branch {BRANCH_WITHOUT_MERGE_QUEUE}: created")


def ensure_ruleset(admin: Actor) -> None:
    rulesets = admin.call_api("GET", f"/repos/{admin.repo}/rulesets")
    if existing := next((r for r in rulesets if r["name"] == RULESET_NAME), None):
        admin.call_api("PUT", f"/repos/{admin.repo}/rulesets/{existing['id']}", RULESET)
        print(f"ruleset {RULESET_NAME}: updated")
    else:
        admin.call_api("POST", f"/repos/{admin.repo}/rulesets", RULESET)
        print(f"ruleset {RULESET_NAME}: created")


def main() -> None:
    token = subprocess.run(
        ["gh", "auth", "token", "--hostname", "github.com"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    admin = Actor("admin", token, f"{os.environ['E2E_ORG']}/{os.environ['E2E_REPO']}")
    repo = admin.call_api("GET", f"/repos/{admin.repo}")
    if repo["private"]:
        raise SystemExit(
            "the test repository must be public for merge queues on a free plan"
        )
    admin.call_api(
        "PATCH",
        f"/repos/{admin.repo}",
        {"allow_squash_merge": True, "delete_branch_on_merge": True},
    )
    # Before the ruleset, which would otherwise demand a merge queue for these commits.
    ensure_workflows(admin, repo["default_branch"])
    ensure_branch_without_merge_queue(admin, repo["default_branch"])
    ensure_ruleset(admin)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
