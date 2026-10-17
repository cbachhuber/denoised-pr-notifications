"""Microsoft Teams card formatting and webhook communication."""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from denoised_pr_notifications.config import config
from denoised_pr_notifications.events import Event, EventKind


def post_card(webhook_url: str, card: dict[str, Any]) -> tuple[int, bytes]:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(card).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read()


def post_cards(webhook_url: str, events: list[Event]) -> dict[str, int]:
    """Post one card per event, spaced out, and return the HTTP status by event URL."""
    statuses: dict[str, int] = {}
    for index, event in enumerate(events):
        if index:
            time.sleep(config.POST_SPACING)
        # 202 only means the Workflows webhook queued it; the flow can still fail afterwards.
        status, body = post_card(webhook_url, build_card(event))
        statuses[event.url] = status
        if body.strip():
            print(f"webhook replied {status}: {body[:200]!r}")
    return statuses


# Verdicts and merges are terse links; actual comment text is worth showing in full.
TERSE_VERBS = {
    EventKind.REVIEW_APPROVED: "approved",
    EventKind.REVIEW_CHANGES_REQUESTED: "requested changes on",
    EventKind.REVIEW_COMMENTED: "commented on",
    EventKind.MERGED: "merged",
}


def format_message(event: Event) -> str:
    actor, url, title = event.actor, event.url, event.pr_title
    if verb := TERSE_VERBS.get(event.kind):
        return f"[{actor} {verb} {title}]({url})"
    return f"{actor} [commented on {title}]({url})\n{event.preview}"


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


def build_card(event: Event) -> dict[str, Any]:
    text = format_message(event)
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


def send_alert(webhook_url: str, message: str, state_dir: Path) -> None:
    """Report a broken notifier, at most once an hour so a minutely schedule cannot spam the channel."""
    stamp = state_dir / "last-alert"
    if (
        stamp.exists()
        and datetime.now(UTC) - datetime.fromtimestamp(stamp.stat().st_mtime, UTC)
        < config.ALERT_INTERVAL
    ):
        return
    post_card(webhook_url, build_alert_card(message))
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
