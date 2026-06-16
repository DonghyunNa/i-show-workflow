#!/bin/bash
# Fast synchronous logger — wraps stdin JSON with timestamp + pid.
# ~17ms vs 35ms for Python. Used for PreToolUse where blocking is acceptable.
LOG_DIR="$HOME/.claude/logs"
mkdir -p "$LOG_DIR"
input=$(cat)
printf '{"ts":%s,"pid":%s,"payload":%s}\n' "$(date +%s)" "$$" "$input" >> "$LOG_DIR/events.jsonl"
