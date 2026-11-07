"""Each scenario opens its own pull request, acts on it, and declares exactly which cards it should cause."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from actors import Actor, Posted, PullRequest

DIRECT_BASE = "e2e-direct"


@dataclass(frozen=True)
class Cast:
    me: Actor
    alice: Actor
    bob: Actor
    run_id: str
    run_link: str

    def open(
        self, author: Actor, scenario: str, base: str = DIRECT_BASE
    ) -> PullRequest:
        return author.open_pull_request(
            branch=f"e2e/{self.run_id}/{scenario}",
            base=base,
            title=f"[e2e {self.run_id}] {scenario}",
            body=f"Opened by {self.run_link}",
        )


@dataclass(frozen=True)
class Expectation:
    pull: PullRequest
    reported: list[Posted]


def tick() -> None:
    """GitHub timestamps have whole seconds, and "after I joined" must be strictly later."""
    time.sleep(1.1)


def comment_on_my_pr(cast: Cast) -> Expectation:
    pull = cast.open(cast.me, "comment_on_my_pr")
    return Expectation(pull, [cast.alice.comment(pull, "A comment on your PR")])


def my_own_comment_on_my_pr(cast: Cast) -> Expectation:
    pull = cast.open(cast.me, "my_own_comment_on_my_pr")
    cast.me.comment(pull, "Talking to myself")
    return Expectation(pull, [])


def conversation_i_joined_late(cast: Cast) -> Expectation:
    pull = cast.open(cast.alice, "conversation_i_joined_late")
    cast.bob.comment(pull, "Before you got here")
    tick()
    cast.me.comment(pull, "Joining in")
    tick()
    return Expectation(pull, [cast.bob.comment(pull, "After you joined")])


SCENARIOS: dict[str, Callable[[Cast], Expectation]] = {
    scenario.__name__: scenario
    for scenario in (
        comment_on_my_pr,
        my_own_comment_on_my_pr,
        conversation_i_joined_late,
    )
}

# Where pr-notify falls short of the README; the reason says why.
KNOWN_GAPS: dict[str, str] = {
    "conversation_i_joined_late": (
        "GitHub search does not count a new comment towards involves: until the pull request itself is "
        "updated, so pr-notify never fetches the conversation I joined"
    ),
}
