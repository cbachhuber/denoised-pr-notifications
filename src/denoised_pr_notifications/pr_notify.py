"""Push activity on pull requests I'm involved in to a Microsoft Teams chat.

Participation is derived from GitHub's ``involves:`` search predicate (author, commenter,
assignee, mentions) rather than from notification ``reason``, which is sticky at
``review_requested`` for CODEOWNERS team requests and can never be filtered afterwards.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

from denoised_pr_notifications import github_api, teams
from denoised_pr_notifications.config import config
from denoised_pr_notifications.events import Event, EventKind
from denoised_pr_notifications.github_api import Comment, PullRequest

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


def collect_events(pull_request: PullRequest, since: datetime, me: str) -> list[Event]:
    """Return the activity on one pull request that happened after ``since`` and is worth telling me about.

    On pull requests I did not author, only threads I am actually in qualify: whether someone else's
    pull request got approved or merged is not my business, even if I reviewed it.
    """

    def is_worth_reporting(login: str) -> bool:
        return login != me and login not in config.IGNORED_AUTHORS

    def is_new(entry: Comment) -> bool:
        return entry.created_at > since and is_worth_reporting(entry.author)

    def is_addressed_to_me(entry: Comment) -> bool:
        return f"@{me}" in entry.body

    is_mine = pull_request.author == me

    def joined_at(entries: list[Comment]) -> datetime | None:
        return min((e.created_at for e in entries if e.author == me), default=None)

    def follows_me(entry: Comment, joined: datetime | None) -> bool:
        """Replies older than my arrival belong to a conversation that was never mine."""
        if is_mine:
            return True
        if joined and entry.created_at > joined:
            return True
        return is_addressed_to_me(entry)

    conversation = pull_request.comments
    events: list[Event] = []

    joined_conversation = joined_at(conversation)
    for comment in conversation:
        if is_new(comment) and follows_me(comment, joined_conversation):
            events.append(Event.from_comment(EventKind.COMMENT, comment))

    for replies in pull_request.review_threads:
        joined_thread = joined_at(replies)
        for comment in replies:
            if is_new(comment) and follows_me(comment, joined_thread):
                events.append(Event.from_comment(EventKind.REVIEW_COMMENT, comment))

    if not is_mine:
        return events

    for review in pull_request.reviews:
        kind = REVIEW_KINDS.get(review.state)
        if (
            kind is None
            or not is_new(review)
            or review.author in config.IGNORED_REVIEW_AUTHORS
        ):
            continue
        # A COMMENTED review with no body is just the envelope around inline comments.
        if review.body or kind is not EventKind.REVIEW_COMMENTED:
            events.append(Event.from_comment(kind, review))

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


def main() -> int:
    token = os.environ["GH_TOKEN"]
    webhook_url = os.environ["TEAMS_WEBHOOK_URL"]

    run_starttime = datetime.now(UTC)
    previous_run_starttime = get_time_of_last_run(config.TIME_OF_LAST_RUN_FILE)
    try:
        me, pull_requests = github_api.fetch_viewer_and_updated_pull_requests(
            token, since=previous_run_starttime
        )
    except github_api.RateLimited as limited:
        # Leaving the last-run time alone means the next successful run will cover this gap.
        print(f"skipped: {limited}")
        return 0

    except urllib.error.HTTPError as error:
        # A dead token would otherwise fail silently in a repository nobody opens.
        if error.code in (401, 403):
            teams.send_alert(webhook_url, f"GitHub rejected the token ({error.code}).")
        return 1

    status_responses: list[int] = []
    sent_notifications = get_previously_sent_notifications(
        run_starttime, config.NOTIFICATION_LOG
    )
    pending_notifications = [
        (pull_request, event)
        for pull_request in pull_requests
        for event in collect_events(
            pull_request, previous_run_starttime - config.SEARCH_LOOKBACK, me
        )
        if event.url not in sent_notifications
    ]
    for index, (pull_request, event) in enumerate(
        sorted(pending_notifications, key=lambda pair: pair[1].at)
    ):
        if index:
            time.sleep(config.POST_SPACING)
        # 202 only means the Workflows webhook queued it; the flow can still fail afterwards.
        status, body, _ = teams.post_card(
            webhook_url, teams.build_card(pull_request, event)
        )
        sent_notifications[event.url] = run_starttime
        status_responses.append(status)
        if body.strip():
            print(f"webhook replied {status}: {body[:200]!r}")

    write_notifications(sent_notifications, config.NOTIFICATION_LOG)
    write_time_of_last_run(run_starttime, config.TIME_OF_LAST_RUN_FILE)
    print(
        f"since={previous_run_starttime.isoformat()} pull_requests={len(pull_requests)} "
        f"notifications={len(status_responses)} webhook_status={sorted(set(status_responses))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
