"""Settings for pr_notify.py."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Final

# Actions sets these to whichever host the workflow runs on, so Enterprise needs no configuration.
SERVER_URL: Final = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
GRAPHQL_URL: Final = (
    os.environ.get("GITHUB_GRAPHQL_URL") or "https://api.github.com/graphql"
)
TOKEN_SETTINGS_URL: Final = (
    os.environ.get("TOKEN_SETTINGS_URL") or f"{SERVER_URL}/settings/tokens"
)
TIME_OF_LAST_RUN_FILE: Final = Path(
    os.environ.get(
        "TIME_OF_LAST_RUN_FILE", Path.home() / ".local/pr-notify/state/time_of_last_run"
    )
)
IGNORED_AUTHORS: Final = frozenset(
    os.environ.get("IGNORED_AUTHORS", "github-actions").split(",")
)
# Suppresses only the review summary; inline comments from the same author are still worth reading.
IGNORED_REVIEW_AUTHORS: Final = frozenset(
    os.environ.get("IGNORED_REVIEW_AUTHORS", "copilot-pull-request-reviewer").split(",")
)
COLD_START_LOOKBACK: Final = timedelta(hours=1)
# GitHub's search index trails reality by seconds, which the last-run time can outrun. Both the search and
# the event window therefore reach back past it, and SEEN_FILE stops the overlap repeating itself.
SEARCH_LOOKBACK: Final = timedelta(minutes=10)
SEEN_RETENTION: Final = timedelta(hours=2)
SEEN_FILE: Final = TIME_OF_LAST_RUN_FILE.with_name("seen")
# Each post starts its own Power Automate run and those race, so cards are spaced rather than fired together.
POST_SPACING: Final = float(os.environ.get("POST_SPACING", "1.5"))
# Minimum interval between alert posts
ALERT_INTERVAL: Final = timedelta(hours=1)
BODY_PREVIEW_CHARS: Final = 400
