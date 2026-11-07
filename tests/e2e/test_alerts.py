"""Scenario 21: a rejected token alerts once through both channels, then stays quiet for the hour."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import Notifier, Sink


def test_rejected_token_alerts_once_per_channel_then_throttles(
    notifier: Notifier, sink: Sink
) -> None:
    recipients = {"e2e-recipient": sink.url("/recipient")}

    first = notifier.run("invalid-token", recipients)

    assert first.returncode == 1, notifier.log
    [recipient_alert] = sink.received("/recipient")
    assert recipient_alert.texts == [
        "PR notifier is broken",
        "GitHub rejected the token (401).",
    ]
    [wrapper_alert] = sink.received("/alerts")
    assert wrapper_alert.texts[0] == "PR notifier is broken"
    assert "exited non-zero" in wrapper_alert.texts[1]

    second = notifier.run("invalid-token", recipients)

    assert second.returncode == 1, notifier.log
    assert len(sink.cards) == 2
