"""Tests for the participation filter that decides what reaches Teams."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from denoised_pr_notifications import pr_notify
from denoised_pr_notifications.github_api import Comment, Merge, PullRequest, Review
from denoised_pr_notifications.pr_notify import collect_events

ME = "chris"
SINCE = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)
BEFORE = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
AFTER = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)


def comment(
    author: str = "colleague", created_at: datetime = AFTER, body: str = "a remark"
) -> Comment:
    return Comment(
        author=author, created_at=created_at, url="https://ghe/c/1", body=body
    )


def review(
    author: str = "colleague",
    created_at: datetime = AFTER,
    body: str = "",
    state: str = "APPROVED",
) -> Review:
    return Review(
        author=author,
        created_at=created_at,
        url="https://ghe/r/1",
        body=body,
        state=state,
    )


def pull_request(**overrides: Any) -> PullRequest:
    base = PullRequest(title="Do the thing", url="https://ghe/pr/42", author=ME)
    return replace(base, **overrides)


def kinds(pr: PullRequest) -> list[str]:
    return [event.kind for event in collect_events(pr, SINCE, ME)]


def test_reports_a_new_comment_from_someone_else() -> None:
    assert kinds(pull_request(comments=[comment()])) == ["Comment"]


@pytest.mark.parametrize(
    ("case", "node"),
    [
        ("my own comment", comment(author=ME)),
        ("a comment older than the last-run time", comment(created_at=BEFORE)),
        ("CI chatter from a bot", comment(author="github-actions")),
    ],
)
def test_ignores_comments_that_are_not_news(case: str, node: Comment) -> None:
    assert kinds(pull_request(comments=[node])) == [], case


def test_reports_a_review_verdict_on_my_pull_request() -> None:
    assert kinds(pull_request(reviews=[review(state="CHANGES_REQUESTED")])) == [
        "Review: Changes Requested"
    ]


def test_ignores_the_empty_envelope_review_wrapping_inline_comments() -> None:
    assert kinds(pull_request(reviews=[review(state="COMMENTED", body="")])) == []


def test_reports_inline_review_comments() -> None:
    threads = [[comment(), comment(author=ME)]]
    assert kinds(pull_request(review_threads=threads)) == ["Review comment"]


def test_reports_a_merge_that_happened_after_the_last_run_time() -> None:
    merged = pull_request(merge=Merge(at=AFTER, by="colleague"))
    assert kinds(merged) == ["Merged"]


def test_reports_my_own_merge_because_the_merge_queue_credits_the_enqueuer() -> None:
    merged = pull_request(merge=Merge(at=AFTER, by=ME))
    assert kinds(merged) == ["Merged"]


def test_ignores_a_merge_that_predates_the_last_run_time() -> None:
    assert kinds(pull_request(merge=Merge(at=BEFORE, by="colleague"))) == []


def test_truncates_long_bodies_but_keeps_the_link() -> None:
    (event,) = collect_events(
        pull_request(comments=[comment(body="x" * 500)]), SINCE, ME
    )
    assert event.preview.endswith("…")
    assert len(event.preview) == 401
    assert event.url == "https://ghe/c/1"


def someone_elses_pull_request(**overrides: Any) -> PullRequest:
    return pull_request(author="colleague", **overrides)


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("an approval", {"reviews": [review(state="APPROVED")]}),
        ("a merge", {"merge": Merge(at=AFTER, by="someone")}),
        (
            "chatter in a conversation I never joined",
            {"comments": [comment()]},
        ),
    ],
)
def test_ignores_activity_on_pull_requests_i_did_not_author(
    case: str, overrides: dict[str, Any]
) -> None:
    assert kinds(someone_elses_pull_request(**overrides)) == [], case


def test_reports_replies_in_a_conversation_i_joined() -> None:
    joined = [comment(author=ME, created_at=BEFORE), comment()]
    assert kinds(someone_elses_pull_request(comments=joined)) == ["Comment"]


def test_reports_replies_only_in_the_review_threads_i_am_part_of() -> None:
    my_thread = [comment(author=ME, created_at=BEFORE), comment()]
    someone_elses_thread = [comment()]
    threads = [my_thread, someone_elses_thread]
    assert kinds(someone_elses_pull_request(review_threads=threads)) == [
        "Review comment"
    ]


def test_ignores_thread_replies_that_predate_my_arrival() -> None:
    # Joining an old thread should not retroactively subscribe me to what was already said in it.
    threads = [[comment(created_at=AFTER), comment(author=ME, created_at=LATER)]]
    assert kinds(someone_elses_pull_request(review_threads=threads)) == []


def test_reports_thread_replies_that_follow_my_arrival() -> None:
    threads = [[comment(author=ME, created_at=AFTER), comment(created_at=LATER)]]
    assert kinds(someone_elses_pull_request(review_threads=threads)) == [
        "Review comment"
    ]


def test_ignores_conversation_comments_that_predate_my_arrival() -> None:
    joined_late = [comment(created_at=AFTER), comment(author=ME, created_at=LATER)]
    assert kinds(someone_elses_pull_request(comments=joined_late)) == []


def test_reports_a_direct_mention_even_where_i_am_a_stranger() -> None:
    summoned = [comment(body=f"could @{ME} weigh in?")]
    assert kinds(someone_elses_pull_request(comments=summoned)) == ["Comment"]


def test_drops_the_copilot_overview_but_keeps_its_inline_comments() -> None:
    overview = review(
        author="copilot-pull-request-reviewer",
        state="COMMENTED",
        body="## Pull request overview",
    )
    inline = [[comment(author="copilot-pull-request-reviewer")]]
    assert kinds(pull_request(reviews=[overview], review_threads=inline)) == [
        "Review comment"
    ]


def test_a_replay_window_overrides_the_last_run_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    state = tmp_path / "time_of_last_run"
    state.write_text("2026-09-08T14:17:21+00:00")

    monkeypatch.setenv("SINCE", "2026-09-08T13:00:00+00:00")
    assert pr_notify.get_time_of_last_run(state) == datetime(
        2026, 9, 8, 13, 0, tzinfo=UTC
    )

    monkeypatch.setenv("SINCE", "")
    assert pr_notify.get_time_of_last_run(state) == datetime(
        2026, 9, 8, 14, 17, 21, tzinfo=UTC
    )


def test_reports_events_in_the_order_they_happened_not_by_category() -> None:
    # Cards race each other through Power Automate, so they must at least leave here in order.
    out_of_order = pull_request(
        comments=[comment(created_at=LATER)],
        reviews=[review(state="APPROVED", created_at=AFTER)],
    )
    assert kinds(out_of_order) == ["Review: Approved", "Comment"]


def test_remembers_what_it_sent_and_forgets_it_once_it_ages_out(tmp_path: Any) -> None:
    now = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    notification_log = tmp_path / "seen"
    pr_notify.write_notifications(
        {
            "https://ghe/fresh": now - timedelta(minutes=5),
            "https://ghe/stale": now - timedelta(hours=3),
        },
        notification_log,
    )

    assert set(pr_notify.get_previously_sent_notifications(now, notification_log)) == {
        "https://ghe/fresh"
    }


def test_writes_last_run_time_as_iso_format(tmp_path: Any) -> None:
    value = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    state = tmp_path / "last-run"

    pr_notify.write_time_of_last_run(value, state)

    assert state.read_text() == value.isoformat()
