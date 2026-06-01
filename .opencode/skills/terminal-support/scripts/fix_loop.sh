#!/usr/bin/env bash
# fix_loop.sh — run ONE iteration of the run-debug-fix loop.
#
# The script:
#   1. runs the given command with a 10-minute timeout
#   2. tees stdout+stderr to .opencode/skills/terminal-support/.last_run.log
#   3. classifies the result with parse_errors.py
#   4. prints a verdict JSON block at the end of stdout, fenced like:
#        ===VERDICT===
#        {...json...}
#        ===END===
#
# opencode owns the outer loop. This script never retries on its own.
#
# Usage:
#   bash .opencode/skills/terminal-support/scripts/fix_loop.sh "<command>"
#   bash .opencode/skills/terminal-support/scripts/fix_loop.sh --timeout 300 "<command>"

set -u

TIMEOUT=600
if [ "${1:-}" = "--timeout" ]; then
  TIMEOUT="$2"
  shift 2
fi

CMD="${1:-}"
if [ -z "$CMD" ]; then
  echo "Usage: fix_loop.sh [--timeout SECS] \"<command>\"" >&2
  exit 64
fi

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$SKILL_DIR/.last_run.log"
PARSE="$SKILL_DIR/scripts/parse_errors.py"

mkdir -p "$SKILL_DIR"
: > "$LOG"

echo "── terminal-support: running ──"
echo "\$ $CMD"
echo "(timeout ${TIMEOUT}s, log: $LOG)"
echo

# Run the command. Use `timeout` if available; otherwise just exec.
if command -v timeout >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  timeout "${TIMEOUT}s" bash -c "$CMD" 2>&1 | tee "$LOG"
  EXIT=${PIPESTATUS[0]}
else
  bash -c "$CMD" 2>&1 | tee "$LOG"
  EXIT=${PIPESTATUS[0]}
fi

echo
echo "── exit code: $EXIT ──"

# Classify.
if [ ! -f "$PARSE" ]; then
  echo "ERROR: parse_errors.py missing at $PARSE" >&2
  exit 2
fi

VERDICT="$(python3 "$PARSE" --exit-code "$EXIT" --log "$LOG" 2>/dev/null || echo '{"verdict":"FATAL","category":"host.fatal","evidence":"parse_errors.py crashed"}')"

echo
echo "===VERDICT==="
echo "$VERDICT"
echo "===END==="

# Always exit 0 — the verdict JSON carries the real signal. Exiting non-zero
# from this wrapper would cause shells to abort opencode's loop and lose the
# verdict.
exit 0
