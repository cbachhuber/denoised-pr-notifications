"""Push activity on pull requests I'm involved in to a Microsoft Teams chat.

Participation is derived from GitHub's ``involves:`` search predicate (author, commenter,
assignee, mentions) rather than from notification ``reason``, which is sticky at
``review_requested`` for CODEOWNERS team requests and can never be filtered afterwards.
"""

from __future__ import annotations

import os
import sys
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from denoised_pr_notifications import github_api, teams
from denoised_pr_notifications.config import config
from denoised_pr_notifications.events import Event, EventKind
from denoised_pr_notifications.github_api import Comment, PullRequest, Review

# Dismissed and pending reviews are deliberately absent: they are not news.
REVIEW_KINDS = {
    "APPROVED": EventKind.REVIEW_APPROVED,
    "CHANGES_REQUESTED": EventKind.REVIEW_CHANGES_REQUESTED,
    "COMMENTED": EventKind.REVIEW_COMMENTED,
}


def get_time_of_last_run(time_of_last_run_file: Path) -> datetime:
    if override := os.environ.get("SINCE", "").strip():
        return datetime.fromisoformat(override)
    if time_of_last_run_file.exists():
        return datetime.fromisoformat(time_of_last_run_file.read_text().strip())
    return datetime.now(UTC) - config.COLD_START_LOOKBACK


def write_time_of_last_run(value: datetime, time_of_last_run_file: Path) -> None:
    time_of_last_run_file.parent.mkdir(parents=True, exist_ok=True)
    time_of_last_run_file.write_text(value.isoformat())


def get_previously_sent_notifications(
    now: datetime, notification_log: Path
) -> dict[str, datetime]:
    """Events already notified, so an overlapping window delivers each one exactly once."""
    if not notification_log.exists():
        return {}
    cutoff = now - config.SEEN_RETENTION
    sent_notifications: dict[str, datetime] = {}
    for line in notification_log.read_text().splitlines():
        stamp, _, url = line.partition("\t")
        if url and datetime.fromisoformat(stamp) > cutoff:
            sent_notifications[url] = datetime.fromisoformat(stamp)
    return sent_notifications


def write_notifications(
    notifications: dict[str, datetime], notification_log: Path
) -> None:
    notification_log.parent.mkdir(parents=True, exist_ok=True)
    notification_log.write_text(
        "".join(f"{stamp.isoformat()}\t{url}\n" for url, stamp in notifications.items())
    )


def review_events(
    reviews: list[Review], is_new_and_worth_reporting: Callable[[Comment], bool]
) -> list[Event]:
    events: list[Event] = []
    for review in reviews:
        kind = REVIEW_KINDS.get(review.state)
        if (
            kind is None
            or not is_new_and_worth_reporting(review)
            or review.author in config.IGNORED_REVIEW_AUTHORS
        ):
            continue
        # A COMMENTED review with no body is just the envelope around inline comments.
        if review.body or kind is not EventKind.REVIEW_COMMENTED:
            events.append(Event.from_comment(kind, review))
    return events


def collect_events(pull_request: PullRequest, since: datetime, me: str) -> list[Event]:
    """Return the activity on one pull request that happened after ``since`` and is worth telling me about.

    On pull requests I did not author, only threads I am actually in qualify: whether someone else's
    pull request got approved or merged is not my business, even if I reviewed it.
    """

    def is_new_and_worth_reporting(entry: Comment) -> bool:
        is_worth_reporting = (
            entry.author != me and entry.author not in config.IGNORED_AUTHORS
        )
        return entry.created_at > since and is_worth_reporting

    def is_addressed_to_me(entry: Comment) -> bool:
        return f"@{me}" in entry.body

    is_my_pr: Final[bool] = pull_request.author == me

    def i_joined_at(entries: list[Comment]) -> datetime | None:
        return min((e.created_at for e in entries if e.author == me), default=None)

    def happened_after_i_joined(entry: Comment, joined: datetime | None) -> bool:
        """Replies older than my arrival belong to a conversation that was never mine."""
        return bool(joined and entry.created_at > joined)

    def new_followed(entries: list[Comment]) -> list[Comment]:
        joined = i_joined_at(entries)
        return [
            e
            for e in entries
            if is_new_and_worth_reporting(e)
            and (
                is_my_pr or happened_after_i_joined(e, joined) or is_addressed_to_me(e)
            )
        ]

    events = [
        Event.from_comment(EventKind.COMMENT, comment)
        for comment in new_followed(pull_request.comments)
    ]
    for replies in pull_request.review_threads:
        events += [
            Event.from_comment(EventKind.REVIEW_COMMENT, comment)
            for comment in new_followed(replies)
        ]

    if is_my_pr:
        events += review_events(pull_request.reviews, is_new_and_worth_reporting)

        merge = pull_request.merge
        # The merge queue credits whoever enqueued the pull request, so "merged by me" is still news.
        if merge and merge.at > since:
            events.append(
                Event(
                    kind=EventKind.MERGED,
                    actor=merge.by,
                    at=merge.at,
                    url=pull_request.url,
                )
            )

    return sorted(events, key=lambda event: event.at)


def filter_pending_notifications(
    pull_requests: list[PullRequest],
    since: datetime,
    me: str,
    sent_notifications: dict[str, datetime],
) -> list[tuple[PullRequest, Event]]:
    """Unsent events across all pull requests, oldest first."""
    pending = [
        (pull_request, event)
        for pull_request in pull_requests
        for event in collect_events(pull_request, since - config.SEARCH_LOOKBACK, me)
        if event.url not in sent_notifications
    ]
    return sorted(pending, key=lambda pair: pair[1].at)


def fetch_viewer_and_updated_pull_requests(
    token: str, webhook_url: str, since: datetime
) -> tuple[str, list[PullRequest]]:
    """Fetch the viewer and updated pull requests; exit cleanly if rate limited, alert on a rejected token."""
    try:
        return github_api.fetch_viewer_and_updated_pull_requests(token, since=since)
    except github_api.RateLimited as limited:
        # Leaving the last-run time alone means the next successful run will cover this gap.
        print(f"skipped: {limited}")
        raise SystemExit(0) from limited
    except urllib.error.HTTPError as error:
        # A dead token would otherwise fail silently in a repository nobody opens.
        if error.code in (401, 403):
            teams.send_alert(webhook_url, f"GitHub rejected the token ({error.code}).")
        raise


def main() -> int:
    token = os.environ["GH_TOKEN"]
    webhook_url = os.environ["TEAMS_WEBHOOK_URL"]

    run_starttime = datetime.now(UTC)
    previous_run_starttime = get_time_of_last_run(config.TIME_OF_LAST_RUN_FILE)
    me, pull_requests = fetch_viewer_and_updated_pull_requests(
        token, webhook_url, previous_run_starttime
    )

    sent_notifications = get_previously_sent_notifications(
        run_starttime, config.NOTIFICATION_LOG
    )
    pending_notifications = filter_pending_notifications(
        pull_requests, previous_run_starttime, me, sent_notifications
    )
    status_by_url = teams.post_cards(webhook_url, pending_notifications)
    sent_notifications |= dict.fromkeys(status_by_url, run_starttime)

    write_notifications(sent_notifications, config.NOTIFICATION_LOG)
    write_time_of_last_run(run_starttime, config.TIME_OF_LAST_RUN_FILE)
    print(
        f"since={previous_run_starttime.isoformat()} pull_requests={len(pull_requests)} "
        f"notifications={len(status_by_url)} webhook_status={sorted(set(status_by_url.values()))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
