"""Tests for the participation filter that decides what reaches Teams."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from pr_notify import collect_events

ME = "chris"
SINCE = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)
BEFORE = "2026-09-08T10:00:00Z"
AFTER = "2026-09-08T12:00:00Z"


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
        ("a comment older than the watermark", comment(created_at=BEFORE)),
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


def test_reports_a_merge_that_happened_after_the_watermark() -> None:
    merged = pull_request(mergedAt=AFTER, mergedBy={"login": "colleague"})
    assert kinds(merged) == ["Merged"]


@pytest.mark.parametrize(
    ("case", "merged_at", "merged_by"),
    [
        ("the merge predates the watermark", BEFORE, "colleague"),
        ("I merged it myself, so it is not news", AFTER, ME),
    ],
)
def test_ignores_merges_that_are_not_news(case: str, merged_at: str, merged_by: str) -> None:
    assert kinds(pull_request(mergedAt=merged_at, mergedBy={"login": merged_by})) == [], case


def test_truncates_long_bodies_but_keeps_the_link() -> None:
    (event,) = collect_events(pull_request(comments={"nodes": [comment(body="x" * 500)]}), SINCE, ME)
    assert event["preview"].endswith("…")
    assert len(event["preview"]) == 401
    assert event["url"] == "https://ghe/c/1"


def test_survives_a_deleted_account() -> None:
    ghosted = comment() | {"author": None}
    (event,) = collect_events(pull_request(comments={"nodes": [ghosted]}), SINCE, ME)
    assert event["actor"] == "ghost"
