"""Push activity on pull requests I'm involved in to a Microsoft Teams chat.

Participation is derived from GitHub's ``involves:`` search predicate (author, commenter,
assignee, mentions) rather than from notification ``reason``, which is sticky at
``review_requested`` for CODEOWNERS team requests and can never be filtered afterwards.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
import traceback
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


def load_recipients(recipients_file: Path) -> dict[str, str]:
    """Webhook URL by GitHub login; rejects entries that cannot be one, before anything runs."""
    recipients = tomllib.loads(recipients_file.read_text())
    for login, webhook_url in recipients.items():
        # The login names a state folder, so it must not be able to escape it.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", login):
            raise ValueError(f"{recipients_file}: {login!r} is not a GitHub login")
        if not (isinstance(webhook_url, str) and webhook_url.startswith("https://")):
            raise ValueError(
                f"{recipients_file}: webhook for {login} must be an https:// URL"
            )
    if not recipients:
        raise ValueError(f"{recipients_file}: lists no recipients")
    return recipients


def state_path_for(path: Path, login: str) -> Path:
    """Each recipient keeps their state in a folder named after them, beside the shared file location."""
    return path.parent / login / path.name


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


def get_review_events(
    reviews: list[Review],
    pr_title: str,
    is_new_and_worth_reporting: Callable[[Comment], bool],
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
            events.append(Event.from_comment(kind, review, pr_title))
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

    def entries_worth_notifying_me_about(entries: list[Comment]) -> list[Comment]:
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
        Event.from_comment(EventKind.COMMENT, comment, pull_request.title)
        for comment in entries_worth_notifying_me_about(pull_request.comments)
    ]
    for replies in pull_request.review_threads:
        events += [
            Event.from_comment(EventKind.REVIEW_COMMENT, comment, pull_request.title)
            for comment in entries_worth_notifying_me_about(replies)
        ]

    if is_my_pr:
        events += get_review_events(
            pull_request.reviews, pull_request.title, is_new_and_worth_reporting
        )

        merge = pull_request.merge
        # The merge queue credits whoever enqueued the pull request, so "merged by me" is still news.
        if merge and merge.at > since:
            events.append(
                Event(
                    kind=EventKind.MERGED,
                    actor=merge.by,
                    at=merge.at,
                    url=pull_request.url,
                    pr_title=pull_request.title,
                )
            )

    return sorted(events, key=lambda event: event.at)


def filter_pending_notifications(
    pull_requests: list[PullRequest],
    since: datetime,
    me: str,
    sent_notifications: dict[str, datetime],
) -> list[Event]:
    """Unsent events across all pull requests, oldest first."""
    pending = [
        event
        for pull_request in pull_requests
        for event in collect_events(pull_request, since - config.SEARCH_LOOKBACK, me)
        if event.url not in sent_notifications
    ]
    return sorted(pending, key=lambda event: event.at)


def fetch_updated_pull_requests(
    token: str,
    webhook_url: str,
    since: datetime,
    login: str,
    state_dir: Path,
) -> list[PullRequest]:
    """Fetch updated pull requests; exit cleanly if rate limited, alert on a rejected token."""
    try:
        return github_api.fetch_updated_pull_requests(token, login, since)
    except github_api.RateLimited as limited:
        # The budget belongs to the token, so every other recipient would hit it too. Leaving the
        # last-run times alone means the next successful run will cover this gap.
        print(f"skipped: {limited}")
        raise SystemExit(0) from limited
    except urllib.error.HTTPError as error:
        # A dead token would otherwise fail silently in a repository nobody opens.
        if error.code in (401, 403):
            teams.send_alert(
                webhook_url,
                f"GitHub rejected the token ({error.code}).",
                state_dir,
            )
        raise


def notify(token: str, login: str, webhook_url: str) -> None:
    time_of_last_run_file = state_path_for(config.TIME_OF_LAST_RUN_FILE, login)
    notification_log = state_path_for(config.NOTIFICATION_LOG, login)

    run_starttime = datetime.now(UTC)
    previous_run_starttime = get_time_of_last_run(time_of_last_run_file)
    pull_requests = fetch_updated_pull_requests(
        token,
        webhook_url,
        previous_run_starttime,
        login,
        time_of_last_run_file.parent,
    )

    sent_notifications = get_previously_sent_notifications(
        run_starttime, notification_log
    )
    pending_notifications = filter_pending_notifications(
        pull_requests, previous_run_starttime, login, sent_notifications
    )
    status_by_url = teams.post_cards(webhook_url, pending_notifications)
    sent_notifications |= dict.fromkeys(status_by_url, run_starttime)

    write_notifications(sent_notifications, notification_log)
    write_time_of_last_run(run_starttime, time_of_last_run_file)
    print(
        f"{login}: since={previous_run_starttime.isoformat()} pull_requests={len(pull_requests)} "
        f"notifications={len(status_by_url)} webhook_status={sorted(set(status_by_url.values()))}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "recipients_file",
        type=Path,
        help="TOML file mapping each GitHub login to its webhook URL",
    )
    args = parser.parse_args()
    token = os.environ["GH_TOKEN"]
    failed: list[str] = []
    for login, webhook_url in load_recipients(args.recipients_file).items():
        try:
            notify(token, login, webhook_url)
        except Exception:  # noqa: BLE001
            # One recipient's dead webhook must not silence everyone else.
            traceback.print_exc()
            failed.append(login)
    if failed:
        print(f"failed for: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
