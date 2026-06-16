#!/usr/bin/env bash
# Remove i-show-workflow from ~/.claude/ — leaves logs in place by default.
#   --purge-logs   also delete ~/.claude/logs/events.jsonl
set -euo pipefail

PURGE_LOGS=0
for arg in "$@"; do
  case "$arg" in
    --purge-logs) PURGE_LOGS=1 ;;
    --help|-h)    echo "Usage: ./uninstall.sh [--purge-logs]"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 1 ;;
  esac
done

CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"

command -v jq >/dev/null || { echo "jq is required (brew install jq)" >&2; exit 1; }

# 1) remove symlinks (only ours)
for link in \
  "$CLAUDE_DIR/hooks/dashboard-log.sh" \
  "$CLAUDE_DIR/hooks/dashboard-log-async.sh" \
  "$CLAUDE_DIR/tools/claude-watch/watch.py"; do
  if [ -L "$link" ]; then rm "$link"; echo "  removed $link"; fi
done
# empty viewer dir
rmdir "$CLAUDE_DIR/tools/claude-watch" 2>/dev/null || true

# 2) settings.json — strip our hook entries
if [ -f "$SETTINGS" ]; then
  BACKUP="$SETTINGS.bak.$(date +%s)"
  cp "$SETTINGS" "$BACKUP"
  jq '
    def strip_ours($key):
      .hooks[$key] = ((.hooks[$key] // [])
        | map(select(
            [ .hooks[]?.command // "" ]
            | any(test("dashboard-log"))
            | not)));

    if .hooks == null then . else
      strip_ours("UserPromptSubmit")
      | strip_ours("PreToolUse")
      | strip_ours("PostToolUse")
      | strip_ours("Stop")
      | strip_ours("SubagentStop")
      # drop empty arrays
      | .hooks |= with_entries(select(.value | length > 0))
      # drop empty hooks object
      | if (.hooks | length) == 0 then del(.hooks) else . end
    end
  ' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
  echo "  settings cleaned: $SETTINGS (backup: $BACKUP)"
fi

# 3) optional: purge logs
if [ "$PURGE_LOGS" -eq 1 ]; then
  rm -f "$CLAUDE_DIR/logs/events.jsonl"
  echo "  logs deleted: $CLAUDE_DIR/logs/events.jsonl"
fi

echo "uninstalled."
