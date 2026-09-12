#!/bin/sh
# Runs the notifier under cron, which has nowhere to report failures to.
set -u

PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export PATH

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="${ENV_FILE:-$HOME/.config/pr-notify/env}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/pr-notify}"
LOG="$STATE_DIR/log"
ALERT_STAMP="$STATE_DIR/last-wrapper-alert"

mkdir -p "$STATE_DIR"
# A line per minute grows without bound, and failures add a traceback each time.
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 262144 ]; then
    tail -n 10000 "$LOG" >"$LOG.new" && mv "$LOG.new" "$LOG"
fi

# set -a: sourcing alone would leave these as shell variables, invisible to the child process.
set -a
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"
set +a

# An unquoted webhook URL contains &, which backgrounds the assignment and silently yields nothing.
if [ -z "${TEAMS_WEBHOOK_URL:-}" ]; then
    echo "$(date -Is) TEAMS_WEBHOOK_URL is empty; single-quote the value in $ENV_FILE" >>"$LOG"
    exit 1
fi

STATE_FILE="${STATE_FILE:-$STATE_DIR/watermark}"
export STATE_FILE
if [ -z "${GH_TOKEN:-}" ]; then
    if [ -n "${GH_HOST:-}" ]; then
        GH_TOKEN=$(gh auth token --hostname "$GH_HOST" 2>/dev/null)
    else
        GH_TOKEN=$(gh auth token 2>/dev/null)
    fi
    export GH_TOKEN
fi
# Outside Actions nothing supplies these, and the github.com fallback would reject an Enterprise token.
if [ -n "${GH_HOST:-}" ]; then
    export GITHUB_SERVER_URL="${GITHUB_SERVER_URL:-https://$GH_HOST}"
    export GITHUB_GRAPHQL_URL="${GITHUB_GRAPHQL_URL:-https://api.$GH_HOST/graphql}"
fi

if uv run "$BASE_DIR/pr_notify.py" >>"$LOG" 2>&1; then
    exit 0
fi

echo "$(date -Is) run failed" >>"$LOG"

# pr_notify.py reports its own auth failures; this covers the cases that stop it starting at all.
if [ -n "${TEAMS_WEBHOOK_URL:-}" ] && [ -z "$(find "$ALERT_STAMP" -mmin -60 2>/dev/null)" ]; then
    curl -sS -X POST "$TEAMS_WEBHOOK_URL" -H 'Content-Type: application/json' -d '{
      "type": "message",
      "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
          "type": "AdaptiveCard",
          "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
          "version": "1.4",
          "body": [
            {"type": "TextBlock", "text": "PR notifier is broken", "weight": "Bolder", "size": "Medium", "color": "Attention"},
            {"type": "TextBlock", "text": "The scheduled run exited non-zero. See ~/.local/state/pr-notify/log.", "wrap": true}
          ]
        }
      }]
    }' >>"$LOG" 2>&1
    touch "$ALERT_STAMP"
fi

exit 1
