"""Microsoft Teams card formatting and webhook communication."""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from typing import Any

from config import config


def post_card(webhook_url: str, card: dict[str, Any]) -> tuple[int, bytes, Any]:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(card).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read(), response.headers


# Verdicts and merges are terse links; actual comment text is worth showing in full.
REVIEW_VERBS = {
    "Approved": "approved",
    "Changes Requested": "requested changes on",
    "Commented": "commented on",
}


def format_message(pull_request: dict[str, Any], event: dict[str, str]) -> str:
    actor, url, title = event["actor"], event["url"], pull_request["title"]
    if event["kind"] == "Merged":
        return f"[{actor} merged {title}]({url})"
    if verb := REVIEW_VERBS.get(event["kind"].removeprefix("Review: ")):
        return f"[{actor} {verb} {title}]({url})"
    return f"{actor} [commented on {title}]({url})\n{event['preview']}"


def wrap_card(
    body: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> dict[str, Any]:
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


def build_card(pull_request: dict[str, Any], event: dict[str, str]) -> dict[str, Any]:
    text = format_message(pull_request, event)
    return wrap_card([{"type": "TextBlock", "text": text, "wrap": True}], [])


def build_alert_card(message: str) -> dict[str, Any]:
    body = [
        {
            "type": "TextBlock",
            "text": "PR notifier is broken",
            "weight": "Bolder",
            "size": "Medium",
            "color": "Attention",
        },
        {"type": "TextBlock", "text": message, "wrap": True},
    ]
    return wrap_card(
        body,
        [
            {
                "type": "Action.OpenUrl",
                "title": "Renew token",
                "url": config.TOKEN_SETTINGS_URL,
            }
        ],
    )


def send_alert(webhook_url: str, message: str) -> None:
    """Report a broken notifier, at most once an hour so a minutely schedule cannot spam the channel."""
    stamp = config.TIME_OF_LAST_RUN_FILE.with_name("last-alert")
    if (
        stamp.exists()
        and datetime.now(UTC) - datetime.fromtimestamp(stamp.stat().st_mtime, UTC)
        < config.ALERT_INTERVAL
    ):
        return
    post_card(webhook_url, build_alert_card(message))
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
