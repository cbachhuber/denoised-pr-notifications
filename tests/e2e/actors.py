"""The people in the end-to-end tests: a machine account for me, and GitHub Apps for everyone else."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import jwt

API = "https://api.github.com"


def request(
    method: str, path: str, token: str, body: dict[str, Any] | None = None
) -> Any:
    req = urllib.request.Request(
        path if path.startswith("https://") else f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"{method} {path} -> {error.code}: {error.read()[:300]!r}"
        ) from error
    return json.loads(payload) if payload.strip() else None


def app_installation_token(app_id: str, private_key: str, repo: str) -> str:
    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key, "RS256"
    )
    installation = request("GET", f"/repos/{repo}/installation", app_jwt)
    return request(
        "POST", f"/app/installations/{installation['id']}/access_tokens", app_jwt
    )["token"]


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str


@dataclass(frozen=True)
class Posted:
    """Something an actor did, as pr-notify will see it through GraphQL."""

    url: str
    actor: str


@dataclass(frozen=True)
class Actor:
    name: str
    token: str
    repo: str

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return request(method, path, self.token, body)

    def as_seen_by_graphql(self, node_id: str) -> Posted:
        # GraphQL drops the "[bot]" suffix REST shows for apps, and pr-notify only ever sees GraphQL.
        node = self.call(
            "POST",
            "/graphql",
            {
                "query": "query($id: ID!) { node(id: $id) { ... on IssueComment { url author { login } }"
                " ... on PullRequestReviewComment { url author { login } }"
                " ... on PullRequestReview { url author { login } } } }",
                "variables": {"id": node_id},
            },
        )["data"]["node"]
        return Posted(url=node["url"], actor=node["author"]["login"])

    def open_pull_request(
        self, branch: str, base: str, title: str, body: str
    ) -> PullRequest:
        base_sha = self.call("GET", f"/repos/{self.repo}/git/ref/heads/{base}")[
            "object"
        ]["sha"]
        self.call(
            "POST",
            f"/repos/{self.repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        self.call(
            "PUT",
            f"/repos/{self.repo}/contents/{branch}.md",
            {
                "message": f"test: {title}",
                "content": base64.b64encode(f"{title}\n".encode()).decode(),
                "branch": branch,
            },
        )
        pull = self.call(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        self.call(
            "POST",
            f"/repos/{self.repo}/issues/{pull['number']}/labels",
            {"labels": ["e2e"]},
        )
        return PullRequest(number=pull["number"], url=pull["html_url"], branch=branch)

    def comment(self, pull: PullRequest, body: str) -> Posted:
        comment = self.call(
            "POST", f"/repos/{self.repo}/issues/{pull.number}/comments", {"body": body}
        )
        return self.as_seen_by_graphql(comment["node_id"])

    def close(self, pull: PullRequest) -> None:
        self.call(
            "PATCH", f"/repos/{self.repo}/pulls/{pull.number}", {"state": "closed"}
        )
        try:
            self.call("DELETE", f"/repos/{self.repo}/git/refs/heads/{pull.branch}")
        except RuntimeError as error:
            # Already gone when the repository deletes merged branches itself.
            if "-> 422" not in str(error):
                raise
