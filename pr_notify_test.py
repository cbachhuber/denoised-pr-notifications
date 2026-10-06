"""Tests for the participation filter that decides what reaches Teams."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

import pr_notify
from pr_notify import build_search_query, collect_events

ME = "chris"
SINCE = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)
BEFORE = "2026-09-08T10:00:00Z"
AFTER = "2026-09-08T12:00:00Z"
LATER = "2026-09-08T13:00:00Z"


def comment(author: str = "colleague", created_at: str = AFTER, body: str = "a remark") -> dict[str, Any]:
    return {"createdAt": created_at, "url": "https://ghe/c/1", "body": body, "author": {"login": author}}


def review(author: str = "colleague", created_at: str = AFTER, body: str = "", state: str = "APPROVED") -> dict[str, Any]:
    return {"createdAt": created_at, "url": "https://ghe/r/1", "body": body, "state": state, "author": {"login": author}}


def pull_request(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 42,
        "title": "Do the thing",
        "url": "https://ghe/pr/42",
        "mergedAt": None,
        "mergedBy": None,
        "author": {"login": ME},
        "repository": {"nameWithOwner": "foo/repo"},
        "comments": {"nodes": []},
        "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
    }
    return base | overrides


def kinds(pr: dict[str, Any]) -> list[str]:
    return [event["kind"] for event in collect_events(pr, SINCE, ME)]


def test_reports_a_new_comment_from_someone_else() -> None:
    assert kinds(pull_request(comments={"nodes": [comment()]})) == ["Comment"]


@pytest.mark.parametrize(
    ("case", "node"),
    [
        ("my own comment", comment(author=ME)),
        ("a comment older than the last-run time", comment(created_at=BEFORE)),
        ("CI chatter from a bot", comment(author="github-actions")),
    ],
)
def test_ignores_comments_that_are_not_news(case: str, node: dict[str, Any]) -> None:
    assert kinds(pull_request(comments={"nodes": [node]})) == [], case


def test_reports_a_review_verdict_on_my_pull_request() -> None:
    assert kinds(pull_request(reviews={"nodes": [review(state="CHANGES_REQUESTED")]})) == ["Review: Changes Requested"]


def test_ignores_the_empty_envelope_review_wrapping_inline_comments() -> None:
    assert kinds(pull_request(reviews={"nodes": [review(state="COMMENTED", body="")]})) == []


def test_reports_inline_review_comments() -> None:
    threads = {"nodes": [{"comments": {"nodes": [comment(), comment(author=ME)]}}]}
    assert kinds(pull_request(reviewThreads=threads)) == ["Review comment"]


def test_reports_a_merge_that_happened_after_the_last_run_time() -> None:
    merged = pull_request(mergedAt=AFTER, mergedBy={"login": "colleague"})
    assert kinds(merged) == ["Merged"]


def test_reports_my_own_merge_because_the_merge_queue_credits_the_enqueuer() -> None:
    merged = pull_request(mergedAt=AFTER, mergedBy={"login": ME})
    assert kinds(merged) == ["Merged"]


def test_ignores_a_merge_that_predates_the_last_run_time() -> None:
    assert kinds(pull_request(mergedAt=BEFORE, mergedBy={"login": "colleague"})) == []


def test_truncates_long_bodies_but_keeps_the_link() -> None:
    (event,) = collect_events(pull_request(comments={"nodes": [comment(body="x" * 500)]}), SINCE, ME)
    assert event["preview"].endswith("…")
    assert len(event["preview"]) == 401
    assert event["url"] == "https://ghe/c/1"


def test_survives_a_deleted_account() -> None:
    ghosted = comment() | {"author": None}
    (event,) = collect_events(pull_request(comments={"nodes": [ghosted]}), SINCE, ME)
    assert event["actor"] == "ghost"


def test_the_search_excludes_review_requests() -> None:
    assert "involves:@me" in build_search_query(SINCE)
    assert "review-requested" not in build_search_query(SINCE)


def someone_elses_pull_request(**overrides: Any) -> dict[str, Any]:
    return pull_request(author={"login": "colleague"}, **overrides)


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("an approval", {"reviews": {"nodes": [review(state="APPROVED")]}}),
        ("a merge", {"mergedAt": AFTER, "mergedBy": {"login": "someone"}}),
        ("chatter in a conversation I never joined", {"comments": {"nodes": [comment()]}}),
    ],
)
def test_ignores_activity_on_pull_requests_i_did_not_author(case: str, overrides: dict[str, Any]) -> None:
    assert kinds(someone_elses_pull_request(**overrides)) == [], case


def test_reports_replies_in_a_conversation_i_joined() -> None:
    joined = {"nodes": [comment(author=ME, created_at=BEFORE), comment()]}
    assert kinds(someone_elses_pull_request(comments=joined)) == ["Comment"]


def test_reports_replies_only_in_the_review_threads_i_am_part_of() -> None:
    my_thread = {"comments": {"nodes": [comment(author=ME, created_at=BEFORE), comment()]}}
    someone_elses_thread = {"comments": {"nodes": [comment()]}}
    threads = {"nodes": [my_thread, someone_elses_thread]}
    assert kinds(someone_elses_pull_request(reviewThreads=threads)) == ["Review comment"]


def test_ignores_thread_replies_that_predate_my_arrival() -> None:
    # Joining an old thread should not retroactively subscribe me to what was already said in it.
    already_there = comment(created_at=AFTER)
    threads = {"nodes": [{"comments": {"nodes": [already_there, comment(author=ME, created_at=LATER)]}}]}
    assert kinds(someone_elses_pull_request(reviewThreads=threads)) == []


def test_reports_thread_replies_that_follow_my_arrival() -> None:
    threads = {"nodes": [{"comments": {"nodes": [comment(author=ME, created_at=AFTER), comment(created_at=LATER)]}}]}
    assert kinds(someone_elses_pull_request(reviewThreads=threads)) == ["Review comment"]


def test_ignores_conversation_comments_that_predate_my_arrival() -> None:
    joined_late = {"nodes": [comment(created_at=AFTER), comment(author=ME, created_at=LATER)]}
    assert kinds(someone_elses_pull_request(comments=joined_late)) == []


def test_reports_a_direct_mention_even_where_i_am_a_stranger() -> None:
    summoned = {"nodes": [comment(body=f"could @{ME} weigh in?")]}
    assert kinds(someone_elses_pull_request(comments=summoned)) == ["Comment"]


def test_drops_the_copilot_overview_but_keeps_its_inline_comments() -> None:
    overview = review(author="copilot-pull-request-reviewer", state="COMMENTED", body="## Pull request overview")
    inline = {"nodes": [{"comments": {"nodes": [comment(author="copilot-pull-request-reviewer")]}}]}
    assert kinds(pull_request(reviews={"nodes": [overview]}, reviewThreads=inline)) == ["Review comment"]


def test_a_replay_window_overrides_the_last_run_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    state = tmp_path / "time_of_last_run"
    state.write_text("2026-09-08T14:17:21+00:00")
    monkeypatch.setattr(pr_notify, "TIME_OF_LAST_RUN_FILE", state)

    monkeypatch.setenv("SINCE", "2026-09-08T13:00:00+00:00")
    assert pr_notify.read_time_of_last_run() == datetime(2026, 9, 8, 13, 0, tzinfo=UTC)

    monkeypatch.setenv("SINCE", "")
    assert pr_notify.read_time_of_last_run() == datetime(2026, 9, 8, 14, 17, 21, tzinfo=UTC)


def test_the_search_reaches_back_further_than_the_last_run_time() -> None:
    # A pull request indexed late must stay searchable after its events have aged past the last-run time.
    assert "updated:>2026-09-08T10:50:00Z" in build_search_query(SINCE)


def test_reports_events_in_the_order_they_happened_not_by_category() -> None:
    # Cards race each other through Power Automate, so they must at least leave here in order.
    out_of_order = pull_request(
        comments={"nodes": [comment(created_at=LATER)]},
        reviews={"nodes": [review(state="APPROVED", created_at=AFTER)]},
    )
    assert kinds(out_of_order) == ["Review: Approved", "Comment"]


def graphql_replying(payload: dict[str, Any]) -> Any:
    return lambda *_args, **_kwargs: (200, json.dumps(payload).encode(), {})


def test_a_spent_rate_limit_is_distinguishable_from_a_real_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # The budget refills hourly, so a run should skip rather than wake anyone up.
    spent = [{"type": "RATE_LIMIT", "message": "API rate limit already exceeded"}]
    monkeypatch.setattr(pr_notify, "post_json", graphql_replying({"errors": spent}))

    with pytest.raises(pr_notify.RateLimited):
        pr_notify.search_pull_requests("token", SINCE)


def test_other_graphql_errors_still_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_notify, "post_json", graphql_replying({"errors": [{"message": "boom"}]}))

    with pytest.raises(RuntimeError) as caught:
        pr_notify.search_pull_requests("token", SINCE)
    assert not isinstance(caught.value, pr_notify.RateLimited)


def test_remembers_what_it_sent_and_forgets_it_once_it_ages_out(    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(pr_notify, "SEEN_FILE", tmp_path / "seen")
    now = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    pr_notify.write_seen({"https://ghe/fresh": now - timedelta(minutes=5), "https://ghe/stale": now - timedelta(hours=3)})

    assert set(pr_notify.read_seen(now)) == {"https://ghe/fresh"}


@pytest.mark.parametrize(
    ("case", "moment"),
    [
        ("already UTC", datetime(2026, 9, 8, 11, 0, tzinfo=UTC)),
        ("ahead of UTC", datetime(2026, 9, 8, 13, 0, tzinfo=timezone(timedelta(hours=2)))),
        ("behind UTC", datetime(2026, 9, 8, 7, 0, tzinfo=timezone(timedelta(hours=-4)))),
    ],
)
def test_the_search_timestamp_is_always_utc(case: str, moment: datetime) -> None:
    assert "updated:>2026-09-08T10:50:00Z" in build_search_query(moment), case
