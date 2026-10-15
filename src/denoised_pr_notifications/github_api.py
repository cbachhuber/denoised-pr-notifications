"""GitHub GraphQL communication for pull request activity."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from denoised_pr_notifications.config import config

QUERY = """
query($q: String!) {
  viewer { login }
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        title
        url
        mergedAt
        mergedBy { login }
        author { login }
        comments(last: 30) {
          nodes { createdAt url body author { login } }
        }
        reviews(last: 20) {
          nodes { createdAt url body state author { login } }
        }
        reviewThreads(last: 30) {
          nodes {
            comments(last: 20) {
              nodes { createdAt url body author { login } }
            }
          }
        }
      }
    }
  }
}
"""


def _login(node: dict[str, Any] | None) -> str:
    # GraphQL returns null for deleted accounts.
    return (node or {}).get("login") or "ghost"


@dataclass(frozen=True)
class Comment:
    author: str
    created_at: datetime
    url: str
    body: str

    @classmethod
    def from_graphql_node(cls, node: dict[str, Any]) -> Comment:
        return cls(
            author=_login(node["author"]),
            created_at=datetime.fromisoformat(node["createdAt"]),
            url=node["url"],
            body=node["body"] or "",
        )


@dataclass(frozen=True)
class Review(Comment):
    state: str

    @classmethod
    def from_graphql_node(cls, node: dict[str, Any]) -> Review:
        return cls(
            author=_login(node["author"]),
            created_at=datetime.fromisoformat(node["createdAt"]),
            url=node["url"],
            body=node["body"] or "",
            state=node["state"],
        )


@dataclass(frozen=True)
class Merge:
    at: datetime
    by: str


@dataclass(frozen=True)
class PullRequest:
    title: str
    url: str
    author: str
    merge: Merge | None = None
    comments: list[Comment] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    review_threads: list[list[Comment]] = field(default_factory=list)

    @classmethod
    def from_graphql_node(cls, node: dict[str, Any]) -> PullRequest:
        merged_at = node["mergedAt"]
        return cls(
            title=node["title"],
            url=node["url"],
            author=_login(node["author"]),
            merge=(
                Merge(at=datetime.fromisoformat(merged_at), by=_login(node["mergedBy"]))
                if merged_at
                else None
            ),
            comments=[Comment.from_graphql_node(c) for c in node["comments"]["nodes"]],
            reviews=[Review.from_graphql_node(r) for r in node["reviews"]["nodes"]],
            review_threads=[
                [Comment.from_graphql_node(c) for c in thread["comments"]["nodes"]]
                for thread in node["reviewThreads"]["nodes"]
            ],
        )


def post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[bytes, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(), response.headers


def build_search_query(since: datetime) -> str:
    # involves: is author OR commenter OR assignee OR mentions, and deliberately not review-requested,
    # which is what keeps CODEOWNERS team requests out of the feed.
    stamp = (
        (since - config.SEARCH_LOOKBACK).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return f"is:pr involves:@me updated:>{stamp} sort:updated-desc"


class RateLimited(RuntimeError):
    """The hourly GraphQL budget is spent. Self-healing, so a run skips rather than fails."""


def fetch_viewer_and_updated_pull_requests(
    token: str, since: datetime
) -> tuple[str, list[PullRequest]]:
    raw, headers = post_json(
        config.GRAPHQL_URL,
        {"query": QUERY, "variables": {"q": build_search_query(since)}},
        {"Authorization": f"Bearer {token}"},
    )
    # Only present for tokens that actually expire; logged verbatim rather than parsed.
    if expiry := headers.get("github-authentication-token-expiration"):
        print(f"token expires: {expiry}")
    response = json.loads(raw)
    if errors := response.get("errors"):
        if any(error.get("type") == "RATE_LIMIT" for error in errors):
            raise RateLimited(errors[0].get("message", "rate limited"))
        raise RuntimeError(f"GraphQL query failed: {errors}")
    data = response["data"]
    return data["viewer"]["login"], [
        PullRequest.from_graphql_node(node) for node in data["search"]["nodes"] if node
    ]
