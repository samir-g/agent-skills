#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

step="${1:-update}"
case "$step" in
  update) uv run "$SCRIPT_DIR/rss_monitor.py" --config "$CONFIG" update ;;
  poll)   shift; uv run "$SCRIPT_DIR/rss_monitor.py" --config "$CONFIG" poll "$@" ;;
  -h|--help)
    echo "usage: $(basename "$0") [update|poll [poll-flags...]]" >&2
    echo "  update  fetch new entries once (default)" >&2
    echo "  poll    fetch repeatedly (forwards remaining flags to rss_monitor poll)" >&2
    ;;
  *)
    echo "unknown step: $step (expected: update, poll)" >&2
    exit 2
    ;;
esac
