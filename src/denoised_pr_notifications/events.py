"""The unit of news that becomes one Teams notification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import config
from .github_api import Comment


@dataclass(frozen=True)
class Event:
    kind: str
    actor: str
    at: datetime
    url: str
    preview: str = ""

    @classmethod
    def from_comment(cls, kind: str, comment: Comment) -> Event:
        body = comment.body.strip()
        preview = body[: config.BODY_PREVIEW_CHARS] + (
            "…" if len(body) > config.BODY_PREVIEW_CHARS else ""
        )
        return cls(
            kind=kind,
            actor=comment.author,
            at=comment.created_at,
            url=comment.url,
            preview=preview,
        )
