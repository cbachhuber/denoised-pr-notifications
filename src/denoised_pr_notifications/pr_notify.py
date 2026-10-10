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
from typing import Any

from . import github_api, teams
from .config import config


def get_time_of_last_run(time_of_last_run_file: Path) -> datetime:
    if override := os.environ.get("SINCE", "").strip():
        return datetime.fromisoformat(override)
    if time_of_last_run_file.exists():
        return datetime.fromisoformat(time_of_last_run_file.read_text().strip())
    return datetime.now(UTC) - config.COLD_START_LOOKBACK


def write_time_of_last_run(value: datetime, time_of_last_run_file: Path) -> None:
    time_of_last_run_file.parent.mkdir(parents=True, exist_ok=True)
    time_of_last_run_file.write_text(value.isoformat())


def read_seen(now: datetime, seen_file: Path) -> dict[str, datetime]:
    """Events already notified, so an overlapping window delivers each one exactly once."""
    if not seen_file.exists():
        return {}
    cutoff = now - config.SEEN_RETENTION
    seen = {}
    for line in seen_file.read_text().splitlines():
        stamp, _, url = line.partition("\t")
        if url and datetime.fromisoformat(stamp) > cutoff:
            seen[url] = datetime.fromisoformat(stamp)
    return seen


def write_seen(seen: dict[str, datetime], seen_file: Path) -> None:
    seen_file.parent.mkdir(parents=True, exist_ok=True)
    seen_file.write_text(
        "".join(f"{stamp.isoformat()}\t{url}\n" for url, stamp in seen.items())
    )


def login_of(node: dict[str, Any] | None) -> str:
    return (node or {}).get("login") or "ghost"


def collect_events(
    pull_request: dict[str, Any], since: datetime, me: str
) -> list[dict[str, str]]:
    """Return the activity on one pull request that happened after ``since`` and is worth telling me about.

    On pull requests I did not author, only threads I am actually in qualify: whether someone else's
    pull request got approved or merged is not my business, even if I reviewed it.
    """

    def is_worth_reporting(login: str) -> bool:
        return login != me and login not in config.IGNORED_AUTHORS

    def is_new(entry: dict[str, Any]) -> bool:
        return datetime.fromisoformat(
            entry["createdAt"]
        ) > since and is_worth_reporting(login_of(entry.get("author")))

    def is_addressed_to_me(entry: dict[str, Any]) -> bool:
        return f"@{me}" in (entry["body"] or "")

    is_mine = login_of(pull_request["author"]) == me

    def joined_at(entries: list[dict[str, Any]]) -> datetime | None:
        spoke = [
            datetime.fromisoformat(e["createdAt"])
            for e in entries
            if login_of(e["author"]) == me
        ]
        return min(spoke) if spoke else None

    def follows_me(entry: dict[str, Any], joined: datetime | None) -> bool:
        """Replies older than my arrival belong to a conversation that was never mine."""
        if is_mine:
            return True
        if joined and datetime.fromisoformat(entry["createdAt"]) > joined:
            return True
        return is_addressed_to_me(entry)

    conversation = pull_request["comments"]["nodes"]
    events: list[dict[str, str]] = []

    joined_conversation = joined_at(conversation)
    for comment in conversation:
        if is_new(comment) and follows_me(comment, joined_conversation):
            events.append(
                {
                    "kind": "Comment",
                    "actor": login_of(comment["author"]),
                    **_create_body(comment),
                }
            )

    for thread in pull_request["reviewThreads"]["nodes"]:
        replies = thread["comments"]["nodes"]
        joined_thread = joined_at(replies)
        for comment in replies:
            if is_new(comment) and follows_me(comment, joined_thread):
                events.append(
                    {
                        "kind": "Review comment",
                        "actor": login_of(comment["author"]),
                        **_create_body(comment),
                    }
                )

    if not is_mine:
        return events

    for review in pull_request["reviews"]["nodes"]:
        # A COMMENTED review with no body is just the envelope around inline comments.
        if (
            not is_new(review)
            or login_of(review["author"]) in config.IGNORED_REVIEW_AUTHORS
        ):
            continue
        if review["body"] or review["state"] != "COMMENTED":
            kind = f"Review: {review['state'].replace('_', ' ').title()}"
            events.append(
                {
                    "kind": kind,
                    "actor": login_of(review["author"]),
                    **_create_body(review),
                }
            )

    merged_at = pull_request["mergedAt"]
    # The merge queue credits whoever enqueued the pull request, so "merged by me" is still news.
    if merged_at and datetime.fromisoformat(merged_at) > since:
        events.append(
            {
                "kind": "Merged",
                "actor": login_of(pull_request["mergedBy"]),
                "at": merged_at,
                "url": pull_request["url"],
                "preview": "",
            }
        )

    return sorted(events, key=lambda event: event["at"])


def _create_body(entry: dict[str, Any]) -> dict[str, str]:
    body = (entry["body"] or "").strip()
    preview = body[: config.BODY_PREVIEW_CHARS] + (
        "…" if len(body) > config.BODY_PREVIEW_CHARS else ""
    )
    return {"at": entry["createdAt"], "url": entry["url"], "preview": preview}


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

    sent: list[int] = []
    seen = read_seen(run_starttime, config.SEEN_FILE)
    pending = [
        (pull_request, event)
        for pull_request in pull_requests
        for event in collect_events(
            pull_request, previous_run_starttime - config.SEARCH_LOOKBACK, me
        )
        if event["url"] not in seen
    ]
    for index, (pull_request, event) in enumerate(
        sorted(pending, key=lambda pair: pair[1]["at"])
    ):
        if index:
            time.sleep(config.POST_SPACING)
        # 202 only means the Workflows webhook queued it; the flow can still fail afterwards.
        status, body, _ = teams.post_card(
            webhook_url, teams.build_card(pull_request, event)
        )
        seen[event["url"]] = run_starttime
        sent.append(status)
        if body.strip():
            print(f"webhook replied {status}: {body[:200]!r}")

    write_seen(seen, config.SEEN_FILE)
    write_time_of_last_run(run_starttime, config.TIME_OF_LAST_RUN_FILE)
    print(
        f"since={previous_run_starttime.isoformat()} pull_requests={len(pull_requests)} "
        f"notifications={len(sent)} webhook_status={sorted(set(sent))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
