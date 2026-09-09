#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Probe a Teams Workflows webhook with three payload shapes to isolate flow vs payload faults."""

import json
import urllib.error
import urllib.request
from getpass import getpass

CARD_BODY = [
    {"type": "TextBlock", "text": "Probe 1 of 3: full card", "weight": "Bolder", "size": "Medium", "wrap": True},
    {"type": "TextBlock", "text": "foo/repo #1351 - shape used by the real notifier", "isSubtle": True, "wrap": True},
]

PAYLOADS = {
    "1. adaptive card + action (what the notifier sends)": {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": CARD_BODY,
                    "actions": [
                        {"type": "Action.OpenUrl", "title": "Open on GitHub", "url": "https://github.com"}
                    ],
                },
            }
        ],
    },
    "2. adaptive card, no actions": {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": "Probe 2 of 3: bare card", "wrap": True}],
                },
            }
        ],
    },
    "3. plain text": {"text": "Probe 3 of 3: plain text"},
}


def main() -> None:
    url = getpass("Paste the Teams webhook URL (input hidden): ").strip()
    print()
    for label, payload in PAYLOADS.items():
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()[:300].decode(errors="replace").strip()
                print(f"{label}\n    HTTP {response.status} {response.reason}  body={body or '<empty>'}\n")
        except urllib.error.HTTPError as error:
            print(f"{label}\n    HTTP {error.code} {error.reason}  body={error.read()[:300]!r}\n")
        except urllib.error.URLError as error:
            print(f"{label}\n    connection failed: {error.reason}\n")

    print("Now check the chat. Which probes appeared?")
    print("  none      -> the flow is off, failing, or the URL belongs to a different flow")
    print("  3 only    -> the flow does not handle Adaptive Card payloads")
    print("  2 but not 1 -> the Action.OpenUrl block is being rejected")
    print("  all three -> delivery works; the problem is upstream in the notifier")


if __name__ == "__main__":
    main()
