"""Tests for GitHub GraphQL queries and responses."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from denoised_pr_notifications import github_api
from denoised_pr_notifications.github_api import build_search_query

SINCE = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)


def graphql_replying(payload: dict[str, Any]) -> Any:
    def reply(*_args: Any, **_kwargs: Any) -> tuple[bytes, dict[str, str]]:
        return json.dumps(payload).encode(), {}

    return reply


def test_search_uses_involves_without_review_requests() -> None:
    query = build_search_query(SINCE, "alice")

    assert "involves:alice " in query
    assert "review-requested" not in query


def test_search_reaches_back_beyond_the_last_run_time() -> None:
    # A PR indexed late stays searchable after its events age past the last-run time.
    assert "updated:>2026-09-08T10:50:00Z" in build_search_query(SINCE, "alice")


@pytest.mark.parametrize(
    ("case", "moment"),
    [
        ("already UTC", datetime(2026, 9, 8, 11, 0, tzinfo=UTC)),
        (
            "ahead of UTC",
            datetime(2026, 9, 8, 13, 0, tzinfo=timezone(timedelta(hours=2))),
        ),
        (
            "behind UTC",
            datetime(2026, 9, 8, 7, 0, tzinfo=timezone(timedelta(hours=-4))),
        ),
    ],
)
def test_search_timestamp_is_always_utc(case: str, moment: datetime) -> None:
    assert "updated:>2026-09-08T10:50:00Z" in build_search_query(moment, "alice"), case


def test_spent_rate_limit_is_distinguishable_from_a_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spent = [{"type": "RATE_LIMIT", "message": "API rate limit already exceeded"}]
    monkeypatch.setattr(github_api, "post_json", graphql_replying({"errors": spent}))

    with pytest.raises(github_api.RateLimited):
        github_api.fetch_updated_pull_requests("token", "alice", SINCE)


def test_other_graphql_errors_still_fail_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github_api, "post_json", graphql_replying({"errors": [{"message": "boom"}]})
    )

    with pytest.raises(RuntimeError) as caught:
        github_api.fetch_updated_pull_requests("token", "alice", SINCE)
    assert not isinstance(caught.value, github_api.RateLimited)


def test_parses_a_response_with_deleted_accounts_and_empty_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_account_comment = {
        "createdAt": "2026-09-08T12:00:00Z",
        "url": "https://ghe/c/1",
        "body": None,
        "author": None,
    }
    node = {
        "title": "Do the thing",
        "url": "https://ghe/pr/42",
        "mergedAt": "2026-09-08T13:00:00Z",
        "mergedBy": None,
        "author": None,
        "comments": {"nodes": [deleted_account_comment]},
        "reviews": {"nodes": []},
        "reviewThreads": {
            "nodes": [{"comments": {"nodes": [deleted_account_comment]}}]
        },
    }
    payload = {"data": {"search": {"nodes": [node, None]}}}
    monkeypatch.setattr(github_api, "post_json", graphql_replying(payload))

    (pull_request,) = github_api.fetch_updated_pull_requests("token", "alice", SINCE)

    assert pull_request.author == "ghost"
    assert pull_request.merge == github_api.Merge(
        at=datetime(2026, 9, 8, 13, 0, tzinfo=UTC), by="ghost"
    )
    assert pull_request.comments[0].author == "ghost"
    assert pull_request.comments[0].body == ""
    assert len(pull_request.review_threads[0]) == 1
