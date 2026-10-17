#!/bin/sh
# Runs the notifier under cron, which has nowhere to report failures to.
set -u

PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export PATH

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="${ENV_FILE:-$HOME/.config/pr-notify/env}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/pr-notify}"
RECIPIENTS_FILE="$HOME/.config/pr-notify/recipients.toml"
LOG="$STATE_DIR/log"

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

TIME_OF_LAST_RUN_FILE="${TIME_OF_LAST_RUN_FILE:-$STATE_DIR/time_of_last_run}"
export TIME_OF_LAST_RUN_FILE
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

if uv run --project "$BASE_DIR" pr-notify "$RECIPIENTS_FILE" >>"$LOG" 2>&1; then
    exit 0
fi

echo "$(date -Is) run failed" >>"$LOG"
exit 1
