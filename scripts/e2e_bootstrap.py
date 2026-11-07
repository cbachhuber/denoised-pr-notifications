"""Idempotently prepare the end-to-end test repository; run as an admin of it, logged in with `gh`."""

import base64
import json
import os
import re
import subprocess
from typing import Any

ORG = os.environ["E2E_ORG"]
REPO = os.environ["E2E_REPO"]
REPO_PATH = f"repos/{ORG}/{REPO}"
DIRECT_BRANCH = "e2e-direct"
RULESET_NAME = "e2e-merge-queue"
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


def gh(
    path: str, method: str = "GET", body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """Call the REST API through `gh`; successes report 200, or 201 for POST, as gh hides the real code."""
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "-X", method, path]
        + (["--input", "-"] if body is not None else []),
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else None
    if result.returncode == 0:
        return 201 if method == "POST" else 200, payload
    if match := re.search(r"\(HTTP (\d{3})\)", result.stderr):
        return int(match[1]), payload
    raise RuntimeError(result.stderr)


def expect(status: int, body: Any, *ok: int) -> Any:
    if status not in ok:
        raise RuntimeError(f"unexpected {status}: {body}")
    return body


def ensure_label() -> None:
    status, body = gh(
        f"{REPO_PATH}/labels",
        "POST",
        {"name": "e2e", "color": "ededed", "description": "Created by an E2E run"},
    )
    expect(status, body, 201, 422)
    print("label e2e:", "created" if status == 201 else "exists")


def ensure_workflows(default_branch: str) -> None:
    for path, content in WORKFLOWS.items():
        status, existing = gh(f"{REPO_PATH}/contents/{path}?ref={default_branch}")
        expect(status, existing, 200, 404)
        if status == 200 and base64.b64decode(existing["content"]).decode() == content:
            print(f"{path}: up to date")
            continue
        request = {
            "message": f"chore: update {path}",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": default_branch,
        }
        if status == 200:
            request["sha"] = existing["sha"]
        expect(*gh(f"{REPO_PATH}/contents/{path}", "PUT", request), 200, 201)
        print(f"{path}: written")


def ensure_direct_branch(default_branch: str) -> None:
    status, ref = gh(f"{REPO_PATH}/git/ref/heads/{DIRECT_BRANCH}")
    expect(status, ref, 200, 404)
    if status == 200:
        print(f"branch {DIRECT_BRANCH}: exists")
        return
    head = expect(*gh(f"{REPO_PATH}/git/ref/heads/{default_branch}"), 200)
    expect(
        *gh(
            f"{REPO_PATH}/git/refs",
            "POST",
            {"ref": f"refs/heads/{DIRECT_BRANCH}", "sha": head["object"]["sha"]},
        ),
        201,
    )
    print(f"branch {DIRECT_BRANCH}: created")


def ensure_ruleset() -> None:
    rulesets = expect(*gh(f"{REPO_PATH}/rulesets"), 200)
    existing = next((r for r in rulesets if r["name"] == RULESET_NAME), None)
    if existing:
        expect(*gh(f"{REPO_PATH}/rulesets/{existing['id']}", "PUT", RULESET), 200)
        print(f"ruleset {RULESET_NAME}: updated")
    else:
        expect(*gh(f"{REPO_PATH}/rulesets", "POST", RULESET), 201)
        print(f"ruleset {RULESET_NAME}: created")


def main() -> None:
    repo = expect(*gh(REPO_PATH), 200)
    if repo["private"]:
        raise SystemExit(
            "the test repository must be public for merge queues on a free plan"
        )
    default_branch = repo["default_branch"]
    expect(
        *gh(
            REPO_PATH,
            "PATCH",
            {"allow_squash_merge": True, "delete_branch_on_merge": True},
        ),
        200,
    )
    ensure_label()
    # Before the ruleset, which would otherwise demand a merge queue for these commits.
    ensure_workflows(default_branch)
    ensure_direct_branch(default_branch)
    ensure_ruleset()


if __name__ == "__main__":
    main()
