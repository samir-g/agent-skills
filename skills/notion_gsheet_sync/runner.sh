#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
uv run "$SCRIPT_DIR/sync.py" --config "$SCRIPT_DIR/config.json"
