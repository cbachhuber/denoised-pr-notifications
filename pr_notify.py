#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Push activity on pull requests I'm involved in to a Microsoft Teams chat.

Participation is derived from GitHub's ``involves:`` search predicate (author, commenter,
assignee, mentions) rather than from notification ``reason``, which is sticky at
``review_requested`` for CODEOWNERS team requests and can never be filtered afterwards.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Actions sets these to whichever host the workflow runs on, so Enterprise needs no configuration.
SERVER_URL = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
GRAPHQL_URL = os.environ.get("GITHUB_GRAPHQL_URL") or "https://api.github.com/graphql"
TOKEN_SETTINGS_URL = os.environ.get("TOKEN_SETTINGS_URL") or f"{SERVER_URL}/settings/tokens"
STATE_FILE = Path(os.environ.get("STATE_FILE", ".state/watermark"))
IGNORED_AUTHORS = frozenset(os.environ.get("IGNORED_AUTHORS", "github-actions").split(","))
# Suppresses only the review summary; inline comments from the same author are still worth reading.
IGNORED_REVIEW_AUTHORS = frozenset(os.environ.get("IGNORED_REVIEW_AUTHORS", "copilot-pull-request-reviewer").split(","))
COLD_START_LOOKBACK = timedelta(hours=1)
ALERT_INTERVAL = timedelta(hours=1)
BODY_PREVIEW_CHARS = 400

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


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, bytes, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read(), response.headers


def read_watermark() -> datetime:
    # Delivery is fire-and-forget, so a replay window is the only way to recover dropped cards.
    if override := os.environ.get("SINCE", "").strip():
        return datetime.fromisoformat(override)
    if STATE_FILE.exists():
        return datetime.fromisoformat(STATE_FILE.read_text().strip())
    return datetime.now(UTC) - COLD_START_LOOKBACK


def write_watermark(value: datetime) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(value.isoformat())


def build_search_query(since: datetime) -> str:
    # involves: is author OR commenter OR assignee OR mentions, and deliberately not review-requested,
    # which is what keeps CODEOWNERS team requests out of the feed.
    stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"is:pr involves:@me updated:>{stamp} sort:updated-desc"


def search_pull_requests(token: str, since: datetime) -> tuple[str, list[dict[str, Any]]]:
    _, raw, headers = post_json(
        GRAPHQL_URL,
        {"query": QUERY, "variables": {"q": build_search_query(since)}},
        {"Authorization": f"Bearer {token}"},
    )
    # Only present for tokens that actually expire; logged verbatim rather than parsed.
    if expiry := headers.get("github-authentication-token-expiration"):
        print(f"token expires: {expiry}")
    response = json.loads(raw)
    if "errors" in response:
        raise RuntimeError(f"GraphQL query failed: {response['errors']}")
    data = response["data"]
    return data["viewer"]["login"], [node for node in data["search"]["nodes"] if node]


def login_of(node: dict[str, Any] | None) -> str:
    return (node or {}).get("login") or "ghost"


def collect_events(pull_request: dict[str, Any], since: datetime, me: str) -> list[dict[str, str]]:
    """Return the activity on one pull request that happened after ``since`` and is worth telling me about.

    On pull requests I did not author, only threads I am actually in qualify: whether someone else's
    pull request got approved or merged is not my business, even if I reviewed it.
    """

    def worth_reporting(login: str) -> bool:
        return login != me and login not in IGNORED_AUTHORS

    def is_new(entry: dict[str, Any]) -> bool:
        return datetime.fromisoformat(entry["createdAt"]) > since and worth_reporting(login_of(entry.get("author")))

    def addressed_to_me(entry: dict[str, Any]) -> bool:
        return f"@{me}" in (entry["body"] or "")

    mine = login_of(pull_request["author"]) == me

    def joined_at(entries: list[dict[str, Any]]) -> datetime | None:
        spoke = [datetime.fromisoformat(e["createdAt"]) for e in entries if login_of(e["author"]) == me]
        return min(spoke) if spoke else None

    def follows_me(entry: dict[str, Any], joined: datetime | None) -> bool:
        """Replies older than my arrival belong to a conversation that was never mine."""
        if mine:
            return True
        if joined and datetime.fromisoformat(entry["createdAt"]) > joined:
            return True
        return addressed_to_me(entry)

    conversation = pull_request["comments"]["nodes"]
    events: list[dict[str, str]] = []

    joined_conversation = joined_at(conversation)
    for comment in conversation:
        if is_new(comment) and follows_me(comment, joined_conversation):
            events.append({"kind": "Comment", "actor": login_of(comment["author"]), **_body(comment)})

    for thread in pull_request["reviewThreads"]["nodes"]:
        replies = thread["comments"]["nodes"]
        joined_thread = joined_at(replies)
        for comment in replies:
            if is_new(comment) and follows_me(comment, joined_thread):
                events.append({"kind": "Review comment", "actor": login_of(comment["author"]), **_body(comment)})

    if not mine:
        return events

    for review in pull_request["reviews"]["nodes"]:
        # A COMMENTED review with no body is just the envelope around inline comments.
        if not is_new(review) or login_of(review["author"]) in IGNORED_REVIEW_AUTHORS:
            continue
        if review["body"] or review["state"] != "COMMENTED":
            kind = f"Review: {review['state'].replace('_', ' ').title()}"
            events.append({"kind": kind, "actor": login_of(review["author"]), **_body(review)})

    merged_at = pull_request["mergedAt"]
    # The merge queue credits whoever enqueued the pull request, so "merged by me" is still news.
    if merged_at and datetime.fromisoformat(merged_at) > since:
        events.append(
            {"kind": "Merged", "actor": login_of(pull_request["mergedBy"]), "url": pull_request["url"], "preview": ""}
        )

    return events


def _body(entry: dict[str, Any]) -> dict[str, str]:
    body = (entry["body"] or "").strip()
    preview = body[:BODY_PREVIEW_CHARS] + ("…" if len(body) > BODY_PREVIEW_CHARS else "")
    return {"url": entry["url"], "preview": preview}


def wrap_card(body: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": body,
                    "actions": actions,
                },
            }
        ],
    }


# Verdicts and merges are terse links; actual comment text is worth showing in full.
REVIEW_VERBS = {"Approved": "approved", "Changes Requested": "requested changes on", "Commented": "commented on"}


def format_message(pull_request: dict[str, Any], event: dict[str, str]) -> str:
    actor, url, title = event["actor"], event["url"], pull_request["title"]
    if event["kind"] == "Merged":
        return f"[{actor} merged {title}]({url})"
    if verb := REVIEW_VERBS.get(event["kind"].removeprefix("Review: ")):
        return f"[{actor} {verb} {title}]({url})"
    return f"{actor} commented: [{event['preview'] or title}]({url})"


def build_card(pull_request: dict[str, Any], event: dict[str, str]) -> dict[str, Any]:
    text = format_message(pull_request, event)
    return wrap_card([{"type": "TextBlock", "text": text, "wrap": True}], [])


def build_alert_card(message: str) -> dict[str, Any]:
    body = [
        {"type": "TextBlock", "text": "PR notifier is broken", "weight": "Bolder", "size": "Medium", "color": "Attention"},
        {"type": "TextBlock", "text": message, "wrap": True},
    ]
    return wrap_card(body, [{"type": "Action.OpenUrl", "title": "Renew token", "url": TOKEN_SETTINGS_URL}])


def alert(webhook_url: str, message: str) -> None:
    """Report a broken notifier, at most once an hour so a minutely schedule cannot spam the channel."""
    stamp = STATE_FILE.with_name("last-alert")
    if stamp.exists() and datetime.now(UTC) - datetime.fromtimestamp(stamp.stat().st_mtime, UTC) < ALERT_INTERVAL:
        return
    post_json(webhook_url, build_alert_card(message), {})
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()


def main() -> None:
    token = os.environ["GH_TOKEN"]
    webhook_url = os.environ["TEAMS_WEBHOOK_URL"]

    run_started_at = datetime.now(UTC)
    since = read_watermark()
    try:
        me, pull_requests = search_pull_requests(token, since)
    except urllib.error.HTTPError as error:
        # A dead token would otherwise fail silently in a repository nobody opens.
        if error.code in (401, 403):
            alert(webhook_url, f"GitHub rejected the token ({error.code}).")
        raise

    sent: list[int] = []
    for pull_request in pull_requests:
        for event in collect_events(pull_request, since, me):
            # 202 only means the Workflows webhook queued it; the flow can still fail afterwards.
            status, body, _ = post_json(webhook_url, build_card(pull_request, event), {})
            sent.append(status)
            if body.strip():
                print(f"webhook replied {status}: {body[:200]!r}")

    write_watermark(run_started_at)
    print(
        f"since={since.isoformat()} pull_requests={len(pull_requests)} "
        f"notifications={len(sent)} webhook_status={sorted(set(sent))}"
    )


if __name__ == "__main__":
    main()
