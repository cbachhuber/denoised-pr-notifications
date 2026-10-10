"""Tests for Microsoft Teams message formatting."""

from __future__ import annotations

import pytest

import teams

PULL_REQUEST = {"title": "Make the change"}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            {
                "kind": "Comment",
                "actor": "alex",
                "url": "https://example/pr/1",
                "preview": "Looks good",
            },
            "alex [commented on Make the change](https://example/pr/1)\nLooks good",
        ),
        (
            {
                "kind": "Review: Approved",
                "actor": "alex",
                "url": "https://example/pr/1",
                "preview": "",
            },
            "[alex approved Make the change](https://example/pr/1)",
        ),
        (
            {
                "kind": "Merged",
                "actor": "alex",
                "url": "https://example/pr/1",
                "preview": "",
            },
            "[alex merged Make the change](https://example/pr/1)",
        ),
    ],
)
def test_formats_activity_message(event: dict[str, str], expected: str) -> None:
    assert teams.format_message(PULL_REQUEST, event) == expected


def test_builds_adaptive_card_for_activity() -> None:
    card = teams.build_card(
        PULL_REQUEST,
        {
            "kind": "Merged",
            "actor": "alex",
            "url": "https://example/pr/1",
            "preview": "",
        },
    )

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
