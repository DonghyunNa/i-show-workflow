#!/usr/bin/env bash
# Install i-show-workflow hooks + TUI viewer into ~/.claude/.
#
# - Creates symlinks (so `git pull` updates the live tool)
# - Registers hooks in ~/.claude/settings.json idempotently
# - Default matcher is "Agent|Task" — only subagent activity is logged.
#   Pass --all-tools to log every tool call instead.
# - Cleans up legacy install paths (dashboard-log.sh, tools/claude-watch/).
set -euo pipefail

MATCHER="Agent|Task"
for arg in "$@"; do
  case "$arg" in
    --all-tools) MATCHER="" ;;
    --help|-h)
      cat <<EOF
Usage: ./install.sh [--all-tools]

  --all-tools   Log every tool call (Bash, Read, Edit, ...).
                Default: only Agent/Task calls.
EOF
      exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 1 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
TOOLS_DIR="$CLAUDE_DIR/tools/i-show-workflow"
LOG_DIR="$CLAUDE_DIR/logs"
SETTINGS="$CLAUDE_DIR/settings.json"

command -v jq >/dev/null || { echo "jq is required (brew install jq)" >&2; exit 1; }

mkdir -p "$HOOKS_DIR" "$TOOLS_DIR" "$LOG_DIR"

# 0) clean up legacy install paths from older versions
for legacy in \
  "$HOOKS_DIR/dashboard-log.sh" \
  "$HOOKS_DIR/dashboard-log-async.sh" \
  "$CLAUDE_DIR/tools/claude-watch/watch.py"; do
  [ -L "$legacy" ] && rm "$legacy"
done
rmdir "$CLAUDE_DIR/tools/claude-watch" 2>/dev/null || true

# 1) symlinks
ln -sfn "$REPO_DIR/hooks/i-show-workflow.sh"       "$HOOKS_DIR/i-show-workflow.sh"
ln -sfn "$REPO_DIR/hooks/i-show-workflow-async.sh" "$HOOKS_DIR/i-show-workflow-async.sh"
ln -sfn "$REPO_DIR/tools/watch.py"                 "$TOOLS_DIR/watch.py"
chmod +x "$REPO_DIR/hooks/"*.sh "$REPO_DIR/tools/watch.py"

# 2) settings.json — backup, then idempotently upsert hook entries
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
BACKUP="$SETTINGS.bak.$(date +%s)"
cp "$SETTINGS" "$BACKUP"

SYNC_CMD="$HOOKS_DIR/i-show-workflow.sh"
ASYNC_CMD="$HOOKS_DIR/i-show-workflow-async.sh"

# Strip pattern matches both the current name and the legacy `dashboard-log`
# name so users upgrading from earlier versions get a clean migration.
jq \
  --arg sync  "$SYNC_CMD" \
  --arg async "$ASYNC_CMD" \
  --arg matcher "$MATCHER" \
  '
  def strip_ours($key):
    .hooks[$key] = ((.hooks[$key] // [])
      | map(select(
          [ .hooks[]?.command // "" ]
          | any(test("i-show-workflow|dashboard-log"))
          | not)));

  def upsert_no_matcher($key; $cmd):
    strip_ours($key)
    | .hooks[$key] += [ {hooks: [ {type: "command", command: $cmd} ]} ];

  def upsert_with_matcher($key; $cmd; $m):
    strip_ours($key)
    | .hooks[$key] += [ {matcher: $m, hooks: [ {type: "command", command: $cmd} ]} ];

  .hooks = (.hooks // {})
  | upsert_no_matcher  ("UserPromptSubmit"; $sync)
  | upsert_with_matcher("PreToolUse";       $sync;  $matcher)
  | upsert_with_matcher("PostToolUse";      $async; $matcher)
  | upsert_no_matcher  ("Stop";             $async)
  | upsert_no_matcher  ("SubagentStop";     $async)
  ' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"

echo "installed."
echo "  repo     : $REPO_DIR"
echo "  hooks    : $HOOKS_DIR/{i-show-workflow,i-show-workflow-async}.sh"
echo "  viewer   : $TOOLS_DIR/watch.py"
echo "  settings : $SETTINGS  (backup: $BACKUP)"
echo "  matcher  : ${MATCHER:-<all tools>}"
echo ""
echo "next:"
echo "  uv run $TOOLS_DIR/watch.py    # launch the dashboard in a new terminal"
