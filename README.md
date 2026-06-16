# i-show-workflow

A live TUI dashboard that shows you which **subagents** are running in your Claude Code sessions, what they're doing, and how long they've been at it — across every open session, in real time.

```
╭─ claude-watch  sessions live: 2   active tools: 1   events: 47 ─────────────╮
╰─────────────────────────────────────────────────────────────────────────────╯
╭─ session 121022c8  last event 2s ago  · my-app ─────────────────────────────╮
│ » fix the auth bug                                                          │
│ ● RUN     Agent      [Explore] locate auth middleware           12.3s       │
│ recent:                                                                     │
│   ✓       Agent      [code-reviewer] verify PR #42              57.1s       │
╰─────────────────────────────────────────────────────────────────────────────╯
```

Claude Code spawns subagents to handle complex tasks — research, code review, parallel exploration — but the CLI doesn't show you what's actually running in real time. This tool taps into the official **hooks API** to stream agent activity into a single terminal.

## Install

```bash
git clone https://github.com/<you>/i-show-workflow.git
cd i-show-workflow
./install.sh
```

Requirements: `bash`, `jq` (`brew install jq`), and [`uv`](https://docs.astral.sh/uv/) (for the viewer; auto-installs `rich`).

The installer is idempotent — re-run it to update or change options.

## Use

In a separate terminal:

```bash
uv run ~/.claude/tools/i-show-workflow/watch.py
```

Quit with `q` or `Ctrl-C`. First run takes a few seconds (uv fetches `rich`); subsequent runs start instantly.

## How it works

```
[Claude Code]
     │ emits PreToolUse / PostToolUse / Stop / SubagentStop events
     ▼
[hooks API]  ← Claude Code's official extension mechanism
     │ runs your registered command, pipes the event JSON over stdin
     ▼
[i-show-workflow.sh]  ← 7-line shell script
     │ appends one line per event to ~/.claude/logs/events.jsonl
     ▼
[~/.claude/logs/events.jsonl]
     ▲
     │ tailed by
     │
[watch.py]  ← rich-based TUI
     │ parses each line into session / tool-call state
     │ re-renders the tree 4× per second
     ▼
[your terminal]
```

Nothing patches Claude Code. We just use the public hooks contract to log events to a file, and read that file from another process.

## What gets logged

By default, the installer registers hooks **only for `Agent` and `Task` tool calls** — i.e. subagent spawns. Routine `Bash` / `Read` / `Edit` calls aren't logged at all, so:

- Performance overhead per agent call: **~8 ms** (sync pre-hook in shell + async post-hook)
- Performance overhead per non-agent tool call: **0 ms** (hook isn't even fired)
- Disk usage: a few KB per agent call

If you want to see what each subagent is doing internally (its own Bash / Read / etc.), re-install with:

```bash
./install.sh --all-tools
```

This widens the matcher to fire on every tool call. Overhead per call rises to ~8 ms (still 88% lower than a naïve Python hook). Subagent activity then shows as indented children under their parent `Agent` row.

## Performance numbers

| Hook strategy            | Per call | Notes |
|--------------------------|---------:|-------|
| naïve Python (cold-start)|     35ms | spawns interpreter every event |
| `python3 -S`             |     33ms | barely helps |
| **pure shell** (this)    |     17ms | sync, used for PreToolUse |
| **shell + async**        |     ~10ms | fire-and-forget, used for Post / Stop |

`Pre` is synchronous so the dashboard sees "tool started" before "tool ended". `Post` / `Stop` / `SubagentStop` are async because they only record completion — no reason to make Claude Code wait.

## Log file

All events go to `~/.claude/logs/events.jsonl` (JSON Lines, one event per line).

⚠️ **The log is plain text and contains everything**: your prompts, tool inputs (file paths, shell commands), `AskUserQuestion` answers. Don't sync the `logs/` directory to cloud backups or share it.

Heavy usage produces ~5 MB/day in default mode (Agent/Task only) or ~50 MB/day in `--all-tools` mode. There's no built-in rotation. A simple daily cron:

```
0 3 * * * gzip ~/.claude/logs/events.jsonl && mv ~/.claude/logs/events.jsonl.gz ~/.claude/logs/events.$(date +\%Y\%m\%d).jsonl.gz 2>/dev/null
```

## Uninstall

```bash
./uninstall.sh             # removes symlinks + hook entries
./uninstall.sh --purge-logs # also delete events.jsonl
```

`settings.json` is backed up before any modification (`settings.json.bak.<timestamp>`).

## FAQ

**Does this require modifying Claude Code?**
No. Hooks are an official, documented feature.

**What if I already have hooks registered?**
Install / uninstall match on the literal strings `i-show-workflow` and `dashboard-log` (the legacy name) in the command path. Your other hooks are untouched.

**Why JSONL on disk instead of a socket / shared memory?**
A file is the simplest contract that survives Claude Code restarts, multiple sessions, and a viewer that comes and goes. The performance ceiling is way higher than what hooks need.

**Why isn't subagent activity in a separate panel?**
Subagents share their parent session's `session_id` in the hook payload, so we use a heuristic: any tool call started while an `Agent` is active is rendered as that agent's child. Reliable in practice because Claude Code's `Agent` invocation is synchronous.

## License

MIT — see [LICENSE](LICENSE).
