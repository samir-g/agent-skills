#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

step="${1:-all}"
case "$step" in
  collect) uv run "$SCRIPT_DIR/collect.py" --config "$CONFIG" ;;
  sync)    uv run "$SCRIPT_DIR/sync.py"    --config "$CONFIG" ;;
  all)
    uv run "$SCRIPT_DIR/collect.py" --config "$CONFIG"
    uv run "$SCRIPT_DIR/sync.py"    --config "$CONFIG"
    ;;
  -h|--help)
    echo "usage: $(basename "$0") [collect|sync|all]" >&2
    echo "  collect  run collect.py only" >&2
    echo "  sync     run sync.py only (reads state.lastArchiveDir written by collect.py)" >&2
    echo "  all      run collect.py then sync.py (default)" >&2
    ;;
  *)
    echo "unknown step: $step (expected: collect, sync, all)" >&2
    exit 2
    ;;
esac
