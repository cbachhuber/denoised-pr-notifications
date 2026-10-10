"""Settings for pr_notify.py."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

_STATE_FOLDER: Final = Path.home() / ".local/pr-notify/state"
_SERVER_URL: Final = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"


# Actions sets these to whichever host the workflow runs on, so Enterprise needs no configuration.
@dataclass(frozen=True)
class Config:
    # Actions sets these to whichever host the workflow runs on, so Enterprise needs no configuration.
    SERVER_URL: str = _SERVER_URL
    GRAPHQL_URL: str = (
        os.environ.get("GITHUB_GRAPHQL_URL") or "https://api.github.com/graphql"
    )
    TOKEN_SETTINGS_URL: str = (
        os.environ.get("TOKEN_SETTINGS_URL") or f"{_SERVER_URL}/settings/tokens"
    )
    TIME_OF_LAST_RUN_FILE: Path = Path(
        os.environ.get("TIME_OF_LAST_RUN_FILE", _STATE_FOLDER / "time_of_last_run")
    )
    # Suppresses only the review summary; inline comments from the same author are still worth reading.
    IGNORED_AUTHORS: frozenset[str] = frozenset(
        os.environ.get("IGNORED_AUTHORS", "github-actions").split(",")
    )
    IGNORED_REVIEW_AUTHORS: frozenset[str] = frozenset(
        os.environ.get("IGNORED_REVIEW_AUTHORS", "copilot-pull-request-reviewer").split(
            ","
        )
    )
    COLD_START_LOOKBACK: timedelta = timedelta(hours=1)
    # GitHub's search index trails reality by seconds, which the last-run time can outrun. Both the search and
    # the event window therefore reach back past it, and SEEN_FILE stops the overlap repeating itself.
    SEARCH_LOOKBACK: timedelta = timedelta(minutes=10)
    SEEN_RETENTION: timedelta = timedelta(hours=2)
    SEEN_FILE: Path = _STATE_FOLDER / "seen"
    # Each post starts its own Power Automate run and those race, so cards are spaced rather than fired together.
    POST_SPACING: float = float(os.environ.get("POST_SPACING", "1.5"))
    # Minimum interval between alert posts
    ALERT_INTERVAL: timedelta = timedelta(hours=1)
    BODY_PREVIEW_CHARS: int = 400


config = Config()
