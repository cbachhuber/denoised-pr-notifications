"""Each scenario opens its own pull request, acts on it, and declares exactly which cards it should cause.

Every function here that takes the run's participants is a scenario; nothing needs registering.
"""

from __future__ import annotations

import inspect
import sys
import time
import typing
from collections.abc import Callable
from dataclasses import dataclass
from types import FunctionType

from actors import ActivitySeenByNotifier, PullRequest, RunParticipants


@dataclass(frozen=True)
class Expectation:
    pull: PullRequest
    reported: list[ActivitySeenByNotifier]


Scenario = Callable[[RunParticipants], Expectation]
ScenarioFunction = typing.TypeVar("ScenarioFunction", bound=FunctionType)
# Filled by @known_gap, by scenario name.
KNOWN_GAPS: dict[str, str] = {}


def known_gap(reason: str) -> Callable[[ScenarioFunction], ScenarioFunction]:
    """Mark a scenario where pr-notify falls short of the README."""

    def mark(scenario: ScenarioFunction) -> ScenarioFunction:
        KNOWN_GAPS[scenario.__name__] = reason
        return scenario

    return mark


def wait_for_next_second() -> None:
    """GitHub timestamps have whole seconds, and "after I joined" must be strictly later."""
    time.sleep(1.1)


def comment_on_my_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "comment_on_my_pr")
    return Expectation(pull, [run.alice.post_comment(pull, "A comment on your PR")])


def my_own_comment_on_my_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "my_own_comment_on_my_pr")
    run.me.post_comment(pull, "Talking to myself")
    return Expectation(pull, [])


def bot_comment_on_my_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "bot_comment_on_my_pr")
    run.me.dispatch_bot_comment(pull, "A comment from CI")
    return Expectation(pull, [])


def inline_comment_on_my_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "inline_comment_on_my_pr")
    return Expectation(
        pull, [run.alice.post_inline_comment(pull, "An inline comment on your PR")]
    )


def approval_of_my_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "approval_of_my_pr")
    return Expectation(pull, [run.alice.submit_review(pull, "APPROVE")])


def changes_requested_on_my_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "changes_requested_on_my_pr")
    review = run.alice.submit_review(pull, "REQUEST_CHANGES", "Please change this")
    return Expectation(pull, [review])


def bodiless_review_wrapping_inline_comments(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "bodiless_review_wrapping_inline_comments")
    comment = run.alice.submit_bodiless_review_with_inline_comment(
        pull, "An inline comment inside a bodiless review"
    )
    return Expectation(pull, [comment])


def dismissed_approval_of_my_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "dismissed_approval_of_my_pr")
    run.alice.submit_review(pull, "APPROVE")
    run.me.dismiss_reviews(pull, "Dismissed by the test")
    # The notifier first looks after the dismissal, so it never sees the approval as such.
    return Expectation(pull, [])


def my_pr_merged_by_someone_else(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.me, "my_pr_merged_by_someone_else")
    return Expectation(pull, [run.alice.merge_pull_request(pull)])


def my_pr_merged_through_merge_queue(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(
        run.me, "my_pr_merged_through_merge_queue", base="main"
    )
    return Expectation(pull, [run.me.merge_through_queue(pull)])


def inline_thread_i_joined_late(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "inline_thread_i_joined_late")
    opening = run.bob.post_inline_comment(pull, "Before you got here")
    wait_for_next_second()
    run.me.reply_to_inline_comment(pull, opening, "Joining in")
    wait_for_next_second()
    reply = run.bob.reply_to_inline_comment(pull, opening, "After you joined")
    return Expectation(pull, [reply])


@known_gap(
    "GitHub search does not count a new comment towards involves: until the pull request itself is "
    "updated, so pr-notify never fetches the conversation I joined (#4)"
)
def conversation_i_joined_late(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "conversation_i_joined_late")
    run.bob.post_comment(pull, "Before you got here")
    wait_for_next_second()
    run.me.post_comment(pull, "Joining in")
    wait_for_next_second()
    return Expectation(pull, [run.bob.post_comment(pull, "After you joined")])


def find_scenarios() -> dict[str, Scenario]:
    module = sys.modules[__name__]
    return {
        name: function
        for name, function in inspect.getmembers(module, inspect.isfunction)
        if function.__module__ == __name__
        and list(typing.get_type_hints(function, vars(module)).values())
        == [RunParticipants, Expectation]
    }


SCENARIOS = find_scenarios()
