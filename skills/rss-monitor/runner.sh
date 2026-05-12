#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
uv run "$SCRIPT_DIR/rss_monitor.py" --config "$SCRIPT_DIR/config.json" update
