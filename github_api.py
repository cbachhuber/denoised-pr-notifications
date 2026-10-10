"""GitHub GraphQL communication for pull request activity."""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from typing import Any

from config import config

QUERY = """
query($q: String!) {
  viewer { login }
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        mergedAt
        mergedBy { login }
        author { login }
        repository { nameWithOwner }
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


def post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[int, bytes, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read(), response.headers


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
) -> tuple[str, list[dict[str, Any]]]:
    _, raw, headers = post_json(
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
    return data["viewer"]["login"], [node for node in data["search"]["nodes"] if node]
