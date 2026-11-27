"""Each scenario opens its own pull request, acts on it, and declares exactly which cards it should cause.

Every function here that takes the run's participants is a scenario; nothing needs registering.
"""

from __future__ import annotations

import inspect
import sys
import time
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FunctionType

from actors import (
    BRANCH_WITHOUT_MERGE_QUEUE,
    ActivitySeenByNotifier,
    PullRequest,
    RunParticipants,
)


@dataclass(frozen=True)
class Expectation:
    pull: PullRequest
    reported: list[ActivitySeenByNotifier]
    # Activity of earlier runs that a run starting within the search lookback may still report.
    leftovers: list[ActivitySeenByNotifier] = field(default_factory=list)


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


def approval_and_merge_of_someone_elses_pr(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "approval_and_merge_of_someone_elses_pr")
    run.bob.submit_review(pull, "APPROVE")
    run.me.merge_pull_request(pull)
    return Expectation(pull, [])


def mention_on_pr_i_am_not_part_of(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "mention_on_pr_i_am_not_part_of")
    mention = run.bob.post_comment(pull, f"@{run.me.name} what do you think?")
    return Expectation(pull, [mention])


@known_gap(
    "is_addressed_to_me compares @<me> case-sensitively, so a mention as @<ME> is dropped (#37)"
)
def mention_with_different_capitalisation(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "mention_with_different_capitalisation")
    mention = run.bob.post_comment(pull, f"@{run.me.name.upper()} what do you think?")
    return Expectation(pull, [mention])


@known_gap(
    "pr-notify never reads the pull request description, so a mention there is not reported (#38)"
)
def mention_in_pr_description(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(
        run.alice,
        "mention_in_pr_description",
        description=f"@{run.me.name} what do you think?",
    )
    return Expectation(pull, [run.alice.fetch_activity_seen_by_notifier(pull.node_id)])


@known_gap(
    "GitHub search does not index @-mentions in review bodies, so pr-notify never fetches the pull request (#36)"
)
def mention_in_review_body(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "mention_in_review_body")
    review = run.bob.submit_review(
        pull, "COMMENT", f"@{run.me.name} what do you think?"
    )
    return Expectation(pull, [review])


@known_gap(
    "GitHub search does not index @-mentions in inline review comments, so pr-notify never fetches the "
    "pull request (#34)"
)
def mention_in_inline_comment_on_pr_i_am_not_part_of(
    run: RunParticipants,
) -> Expectation:
    pull = run.open_pull_request(
        run.alice, "mention_in_inline_comment_on_pr_i_am_not_part_of"
    )
    mention = run.bob.post_inline_comment(pull, f"@{run.me.name} what do you think?")
    return Expectation(pull, [mention])


@known_gap(
    "is_addressed_to_me matches @<me> as a substring, so a mention of @<me>x is reported (#33)"
)
def mention_of_login_starting_with_mine(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "mention_of_login_starting_with_mine")
    # Assigned, so the search finds the pull request even though nobody mentions me.
    run.me.assign(pull, run.me.name)
    run.bob.post_comment(pull, f"@{run.me.name}x what do you think?")
    return Expectation(pull, [])


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


@known_gap(
    "GitHub search does not list pull requests that mention my team, and is_addressed_to_me only "
    "matches @<login> (#40)"
)
def mention_of_team_i_am_in(run: RunParticipants) -> Expectation:
    pull = run.open_pull_request(run.alice, "mention_of_team_i_am_in")
    mention = run.bob.post_comment(pull, f"@{run.team_handle} what do you think?")
    return Expectation(pull, [mention])


def new_comment_on_an_older_pr_of_mine(run: RunParticipants) -> Expectation:
    pull = run.me.find_or_open_long_lived_pull_request(BRANCH_WITHOUT_MERGE_QUEUE)
    earlier = run.alice.list_recent_comments(pull)
    comment = run.alice.post_comment(pull, "A comment on your older PR")
    return Expectation(pull, [comment], leftovers=earlier)


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
