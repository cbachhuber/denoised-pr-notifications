"""The people in the end-to-end tests: a machine account for me, and GitHub Apps for everyone else."""

from __future__ import annotations

import base64
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import pytest
import requests

API = "https://api.github.com"
BRANCH_WITHOUT_MERGE_QUEUE = "branch-without-merge-queue"
BOT_COMMENT_TIMEOUT_SECONDS = 180
MERGE_QUEUE_TIMEOUT_SECONDS = 300
MERGEABLE_TIMEOUT_SECONDS = 60


def wait_until(description: str, is_done: Callable[[], bool], seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_done():
            return
        time.sleep(5)
    raise TimeoutError(f"gave up waiting for {description}")


def require_env(name: str) -> str:
    if not (value := os.environ.get(name)):
        pytest.skip(f"{name} is unset; see CONTRIBUTING.md")
    return value


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str


@dataclass(frozen=True)
class ActivitySeenByNotifier:
    """A comment or review, identified the way pr-notify sees it through GraphQL."""

    url: str
    actor: str


@dataclass(frozen=True)
class Actor:
    name: str
    token: str
    repo: str

    @classmethod
    def from_app(cls, name: str, app_id: str, private_key: str, repo: str) -> Actor:
        now = int(time.time())
        app = cls(
            name,
            jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": app_id},
                private_key,
                "RS256",
            ),
            repo,
        )
        installation = app.call_api("GET", f"/repos/{repo}/installation")
        token = app.call_api(
            "POST", f"/app/installations/{installation['id']}/access_tokens"
        )["token"]
        return cls(name, token, repo)

    def call_api(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        response = requests.request(
            method,
            f"{API}{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
        if not response.ok:
            # GitHub explains its errors in the body, which raise_for_status() would drop.
            raise requests.HTTPError(
                f"{method} {path} -> {response.status_code}: {response.text[:300]}",
                response=response,
            )
        return response.json() if response.content else None

    def fetch_activity_seen_by_notifier(self, node_id: str) -> ActivitySeenByNotifier:
        # GraphQL drops the "[bot]" suffix REST shows for apps, and pr-notify only ever sees GraphQL.
        node = self.call_api(
            "POST",
            "/graphql",
            {
                "query": "query($id: ID!) { node(id: $id) { ... on IssueComment { url author { login } }"
                " ... on PullRequestReviewComment { url author { login } }"
                " ... on PullRequestReview { url author { login } } } }",
                "variables": {"id": node_id},
            },
        )["data"]["node"]
        return ActivitySeenByNotifier(url=node["url"], actor=node["author"]["login"])

    def open_pull_request(
        self, branch: str, base: str, title: str, body: str
    ) -> PullRequest:
        base_sha = self.call_api("GET", f"/repos/{self.repo}/git/ref/heads/{base}")[
            "object"
        ]["sha"]
        self.call_api(
            "POST",
            f"/repos/{self.repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        self.call_api(
            "PUT",
            f"/repos/{self.repo}/contents/{branch}.md",
            {
                "message": f"test: {title}",
                "content": base64.b64encode(f"{title}\n".encode()).decode(),
                "branch": branch,
            },
        )
        pull = self.call_api(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return PullRequest(number=pull["number"], url=pull["html_url"], branch=branch)

    def post_comment(self, pull: PullRequest, body: str) -> ActivitySeenByNotifier:
        comment = self.call_api(
            "POST", f"/repos/{self.repo}/issues/{pull.number}/comments", {"body": body}
        )
        return self.fetch_activity_seen_by_notifier(comment["node_id"])

    def post_inline_comment(
        self, pull: PullRequest, body: str
    ) -> ActivitySeenByNotifier:
        """Comment on the first line of the file the pull request adds."""
        head_sha = self.call_api("GET", f"/repos/{self.repo}/pulls/{pull.number}")[
            "head"
        ]["sha"]
        comment = self.call_api(
            "POST",
            f"/repos/{self.repo}/pulls/{pull.number}/comments",
            {
                "body": body,
                "commit_id": head_sha,
                "path": f"{pull.branch}.md",
                "line": 1,
                "side": "RIGHT",
            },
        )
        return self.fetch_activity_seen_by_notifier(comment["node_id"])

    def submit_review(
        self, pull: PullRequest, event: str, body: str = ""
    ) -> ActivitySeenByNotifier:
        review = self.call_api(
            "POST",
            f"/repos/{self.repo}/pulls/{pull.number}/reviews",
            {"event": event, "body": body},
        )
        return self.fetch_activity_seen_by_notifier(review["node_id"])

    def submit_bodiless_review_with_inline_comment(
        self, pull: PullRequest, inline_body: str
    ) -> ActivitySeenByNotifier:
        """Return the inline comment; the review wrapping it has no body of its own."""
        head_sha = self.call_api("GET", f"/repos/{self.repo}/pulls/{pull.number}")[
            "head"
        ]["sha"]
        review = self.call_api(
            "POST",
            f"/repos/{self.repo}/pulls/{pull.number}/reviews",
            {
                "event": "COMMENT",
                "commit_id": head_sha,
                "comments": [
                    {
                        "path": f"{pull.branch}.md",
                        "line": 1,
                        "side": "RIGHT",
                        "body": inline_body,
                    }
                ],
            },
        )
        (comment,) = self.call_api(
            "GET",
            f"/repos/{self.repo}/pulls/{pull.number}/reviews/{review['id']}/comments",
        )
        return self.fetch_activity_seen_by_notifier(comment["node_id"])

    def dispatch_bot_comment(
        self, pull: PullRequest, body: str
    ) -> ActivitySeenByNotifier:
        """Have github-actions comment through the test repository's bot-comment workflow, and wait for it."""
        default_branch = self.call_api("GET", f"/repos/{self.repo}")["default_branch"]
        self.call_api(
            "POST",
            f"/repos/{self.repo}/actions/workflows/bot-comment.yml/dispatches",
            {"ref": default_branch, "inputs": {"pr": str(pull.number), "body": body}},
        )
        deadline = time.monotonic() + BOT_COMMENT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            comments = self.call_api(
                "GET", f"/repos/{self.repo}/issues/{pull.number}/comments"
            )
            if comment := next(
                (
                    c
                    for c in comments
                    if c["user"]["login"] == "github-actions[bot]" and c["body"] == body
                ),
                None,
            ):
                return self.fetch_activity_seen_by_notifier(comment["node_id"])
            time.sleep(5)
        raise TimeoutError(
            f"no bot comment on {pull.url} after the bot-comment workflow"
        )

    def dismiss_reviews(self, pull: PullRequest, message: str) -> None:
        reviews = self.call_api(
            "GET", f"/repos/{self.repo}/pulls/{pull.number}/reviews"
        )
        for review in reviews:
            if review["state"] in ("APPROVED", "CHANGES_REQUESTED"):
                self.call_api(
                    "PUT",
                    f"/repos/{self.repo}/pulls/{pull.number}/reviews/{review['id']}/dismissals",
                    {"message": message},
                )

    def reply_to_inline_comment(
        self, pull: PullRequest, thread: ActivitySeenByNotifier, body: str
    ) -> ActivitySeenByNotifier:
        # The URL ends in #discussion_r<id>, which is the id the replies endpoint wants.
        comment_id = thread.url.rsplit("#discussion_r", 1)[1]
        reply = self.call_api(
            "POST",
            f"/repos/{self.repo}/pulls/{pull.number}/comments/{comment_id}/replies",
            {"body": body},
        )
        return self.fetch_activity_seen_by_notifier(reply["node_id"])

    def fetch_merge_seen_by_notifier(self, pull: PullRequest) -> ActivitySeenByNotifier:
        owner, name = self.repo.split("/")
        node = self.call_api(
            "POST",
            "/graphql",
            {
                "query": "query($owner: String!, $name: String!, $number: Int!) {"
                " repository(owner: $owner, name: $name) { pullRequest(number: $number) {"
                " url mergedBy { login } } } }",
                "variables": {"owner": owner, "name": name, "number": pull.number},
            },
        )["data"]["repository"]["pullRequest"]
        return ActivitySeenByNotifier(url=node["url"], actor=node["mergedBy"]["login"])

    def assign(self, pull: PullRequest, login: str) -> None:
        self.call_api(
            "POST",
            f"/repos/{self.repo}/issues/{pull.number}/assignees",
            {"assignees": [login]},
        )

    def merge_pull_request(self, pull: PullRequest) -> ActivitySeenByNotifier:
        def try_merge() -> bool:
            try:
                self.call_api(
                    "PUT", f"/repos/{self.repo}/pulls/{pull.number}/merge", {}
                )
            except requests.HTTPError as error:
                # A fresh pull request is refused with 405 while GitHub still computes its mergeability.
                if error.response is None or error.response.status_code != 405:
                    raise
                return False
            return True

        wait_until(
            "the pull request to be mergeable", try_merge, MERGEABLE_TIMEOUT_SECONDS
        )
        return self.fetch_merge_seen_by_notifier(pull)

    def merge_through_queue(self, pull: PullRequest) -> ActivitySeenByNotifier:
        """Enqueue once the required check has passed, and wait for the queue to merge it."""
        details = self.call_api("GET", f"/repos/{self.repo}/pulls/{pull.number}")

        def has_passed_check() -> bool:
            runs = self.call_api(
                "GET", f"/repos/{self.repo}/commits/{details['head']['sha']}/check-runs"
            )["check_runs"]
            passed = any(
                run["name"] == "check" and run["conclusion"] == "success"
                for run in runs
            )
            return passed

        wait_until("the required check", has_passed_check, MERGE_QUEUE_TIMEOUT_SECONDS)
        self.call_api(
            "POST",
            "/graphql",
            {
                "query": "mutation($id: ID!) { enqueuePullRequest(input: {pullRequestId: $id})"
                " { mergeQueueEntry { id } } }",
                "variables": {"id": details["node_id"]},
            },
        )

        def is_merged() -> bool:
            merged = self.call_api("GET", f"/repos/{self.repo}/pulls/{pull.number}")[
                "merged"
            ]
            return merged

        wait_until("the merge queue", is_merged, MERGE_QUEUE_TIMEOUT_SECONDS)
        return self.fetch_merge_seen_by_notifier(pull)

    def close_pull_request(self, pull: PullRequest) -> None:
        self.call_api(
            "PATCH", f"/repos/{self.repo}/pulls/{pull.number}", {"state": "closed"}
        )
        try:
            self.call_api("DELETE", f"/repos/{self.repo}/git/refs/heads/{pull.branch}")
        except requests.HTTPError as error:
            # Already gone when the repository deletes merged branches itself.
            if error.response is None or error.response.status_code != 422:
                raise


@dataclass(frozen=True)
class RunParticipants:
    """Everyone acting in one test run, and how that run identifies itself on the pull requests it opens."""

    me: Actor
    alice: Actor
    bob: Actor
    run_id: str
    run_link: str

    @classmethod
    def from_environment(cls) -> RunParticipants:
        repo = f"{require_env('E2E_ORG')}/{require_env('E2E_REPO')}"

        def create_app_actor(name: str) -> Actor:
            key = (
                os.environ.get(f"E2E_{name}_PRIVATE_KEY")
                or Path(
                    os.path.expandvars(require_env(f"E2E_{name}_PRIVATE_KEY_FILE"))
                ).read_text()
            )
            return Actor.from_app(
                name.lower(), require_env(f"E2E_{name}_APP_ID"), key, repo
            )

        if run_id := os.environ.get("GITHUB_RUN_ID"):
            server, origin = (
                os.environ["GITHUB_SERVER_URL"],
                os.environ["GITHUB_REPOSITORY"],
            )
            run_link = f"{server}/{origin}/actions/runs/{run_id}"
            run_id = f"{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        else:
            run_id = f"local-{secrets.token_hex(3)}"
            run_link = f"local run {run_id}"
        return cls(
            me=Actor(require_env("E2E_ME_LOGIN"), require_env("E2E_TOKEN_ME"), repo),
            alice=create_app_actor("ALICE"),
            bob=create_app_actor("BOB"),
            run_id=run_id,
            run_link=run_link,
        )

    def open_pull_request(
        self, author: Actor, scenario: str, base: str = BRANCH_WITHOUT_MERGE_QUEUE
    ) -> PullRequest:
        return author.open_pull_request(
            branch=f"{self.run_id}/{scenario}",
            base=base,
            title=f"[{self.run_id}] {scenario}",
            body=f"Opened by {self.run_link}",
        )
