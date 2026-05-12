#!/bin/zsh
# Thin wrapper around notion_to_gsheet_sync.py.
#
# Usage: notion_to_gsheet_sync_runner.sh [--config PATH] [--env-file PATH] [extra args...]
#
# If --config is omitted, defaults to config.json next to this script.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
exec python3 "$SCRIPT_DIR/notion_to_gsheet_sync.py" --config "$CONFIG" "${ARGS[@]}"
