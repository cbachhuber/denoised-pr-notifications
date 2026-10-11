"""Tests for Microsoft Teams message formatting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from denoised_pr_notifications import teams
from denoised_pr_notifications.events import Event
from denoised_pr_notifications.github_api import PullRequest

PULL_REQUEST = PullRequest(
    title="Make the change", url="https://example/pr/1", author="alex"
)


def event(kind: str, preview: str = "") -> Event:
    return Event(
        kind=kind,
        actor="alex",
        at=datetime(2026, 1, 1, tzinfo=UTC),
        url="https://example/pr/1",
        preview=preview,
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            event("Comment", "Looks good"),
            "alex [commented on Make the change](https://example/pr/1)\nLooks good",
        ),
        (
            event("Review: Approved"),
            "[alex approved Make the change](https://example/pr/1)",
        ),
        (
            event("Merged"),
            "[alex merged Make the change](https://example/pr/1)",
        ),
    ],
)
def test_formats_activity_message(event: Event, expected: str) -> None:
    assert teams.format_message(PULL_REQUEST, event) == expected


def test_builds_adaptive_card_for_activity() -> None:
    card = teams.build_card(PULL_REQUEST, event("Merged"))

    assert card["type"] == "message"
    attachment = card["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["body"][0]["text"] == (
        "[alex merged Make the change](https://example/pr/1)"
    )


def test_alert_card_links_to_token_settings() -> None:
    card = teams.build_alert_card("GitHub rejected the token (401).")
    content = card["attachments"][0]["content"]

    assert content["body"][1]["text"] == "GitHub rejected the token (401)."
    assert content["actions"][0]["type"] == "Action.OpenUrl"
    assert content["actions"][0]["title"] == "Renew token"
