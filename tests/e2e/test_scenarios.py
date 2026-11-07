"""Every scenario's pull request causes exactly the cards it declared, each once."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from scenarios import KNOWN_GAPS, SCENARIOS

if TYPE_CHECKING:
    from conftest import World


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            name,
            # Not strict: whether the search index has caught up yet is a matter of timing.
            marks=pytest.mark.xfail(reason=KNOWN_GAPS[name], strict=False)
            if name in KNOWN_GAPS
            else (),
        )
        for name in SCENARIOS
    ],
)
def test_scenario(world: World, scenario: str) -> None:
    expectation = world.expectations[scenario]
    pull_url = expectation.pull.url
    received = sorted(
        (card.link, card.actor)
        for card in world.cards
        if card.link == pull_url or card.link.startswith(f"{pull_url}#")
    )
    expected = sorted((posted.url, posted.actor) for posted in expectation.reported)
    assert received == expected, f"{pull_url}\n{world.log}"
