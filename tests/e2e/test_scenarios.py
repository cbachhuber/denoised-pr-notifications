"""Every scenario's pull request causes exactly the cards it declared, each once."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from scenarios import KNOWN_GAPS, OPT_IN, SCENARIOS, is_enabled

if TYPE_CHECKING:
    from conftest import DeliveredNotifications


def mark_scenario(name: str) -> list[pytest.MarkDecorator]:
    marks = []
    if name in KNOWN_GAPS:
        # Not strict: whether the search index has caught up yet is a matter of timing.
        marks.append(pytest.mark.xfail(reason=KNOWN_GAPS[name], strict=False))
    if not is_enabled(name):
        marks.append(pytest.mark.skip(reason=f"{OPT_IN[name]} is unset"))
    return marks


@pytest.mark.parametrize(
    "scenario",
    [pytest.param(name, marks=mark_scenario(name)) for name in SCENARIOS],
)
def test_scenario(
    delivered_notifications: DeliveredNotifications, scenario: str
) -> None:
    expectation = delivered_notifications.expectations[scenario]
    pull_url = expectation.pull.url
    tolerated = {(activity.url, activity.actor) for activity in expectation.leftovers}
    received = sorted(
        (card.link, card.actor)
        for card in delivered_notifications.cards
        if (card.link == pull_url or card.link.startswith(f"{pull_url}#"))
        and (card.link, card.actor) not in tolerated
    )
    expected = sorted(
        (activity.url, activity.actor) for activity in expectation.reported
    )
    assert received == expected, f"{pull_url}\n{delivered_notifications.log}"
