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
    def reply(*_args: Any, **_kwargs: Any) -> tuple[int, bytes, dict[str, str]]:
        return 200, json.dumps(payload).encode(), {}

    return reply


def test_search_uses_involves_without_review_requests() -> None:
    query = build_search_query(SINCE)

    assert "involves:@me" in query
    assert "review-requested" not in query


def test_search_reaches_back_beyond_the_last_run_time() -> None:
    # A PR indexed late stays searchable after its events age past the last-run time.
    assert "updated:>2026-09-08T10:50:00Z" in build_search_query(SINCE)


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
    assert "updated:>2026-09-08T10:50:00Z" in build_search_query(moment), case


def test_spent_rate_limit_is_distinguishable_from_a_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spent = [{"type": "RATE_LIMIT", "message": "API rate limit already exceeded"}]
    monkeypatch.setattr(github_api, "post_json", graphql_replying({"errors": spent}))

    with pytest.raises(github_api.RateLimited):
        github_api.fetch_viewer_and_updated_pull_requests("token", SINCE)


def test_other_graphql_errors_still_fail_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github_api, "post_json", graphql_replying({"errors": [{"message": "boom"}]})
    )

    with pytest.raises(RuntimeError) as caught:
        github_api.fetch_viewer_and_updated_pull_requests("token", SINCE)
    assert not isinstance(caught.value, github_api.RateLimited)
