#!/bin/bash
# Fire-and-forget logger — backgrounds the write so Claude Code doesn't wait.
# Used for PostToolUse/Stop/SubagentStop where blocking has no benefit.
# ~10ms vs 35ms.
input=$(cat)
(
  LOG_DIR="$HOME/.claude/logs"
  mkdir -p "$LOG_DIR"
  ts=$(date +%s)
  printf '{"ts":%s,"pid":%s,"payload":%s}\n' "$ts" "$$" "$input" >> "$LOG_DIR/events.jsonl"
) &
disown 2>/dev/null || true
