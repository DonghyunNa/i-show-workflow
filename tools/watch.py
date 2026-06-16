#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13"]
# ///
"""claude-watch — live TUI dashboard for Claude Code agent activity.

Tails ~/.claude/logs/events.jsonl and renders per-session activity:
  - currently running tools / subagents (yellow, with elapsed time)
  - recently completed tools (dim, with duration)
  - which subagent type is doing what

Run in a separate terminal window while you work in Claude Code:
    uv run ~/.claude/tools/i-show-workflow/watch.py

Keys: q / Ctrl-C to quit.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align

LOG_FILE = Path.home() / ".claude" / "logs" / "events.jsonl"
REFRESH_HZ = 4
MAX_ACTIVE_PER_SESSION = 5    # top-level active tools shown before "... +K more"
MAX_CHILDREN_PER_AGENT = 3    # subagent children shown before "... +K more"
MAX_RECENT_PER_SESSION = 6
SESSION_IDLE_HIDE_SEC = 600   # hide sessions idle > 10 min
SESSION_DELETE_SEC = 3600     # forget sessions idle > 1 hour


@dataclass
class ToolCall:
    key: str
    session_id: str
    tool_name: str
    description: str
    started_at: float
    finished_at: float | None = None
    error: bool = False
    parent_key: str | None = None   # set when this call ran while an Agent was active

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at

    @property
    def is_agent(self) -> bool:
        return self.tool_name in ("Agent", "Task")


@dataclass
class Session:
    session_id: str
    started_at: float
    last_event_at: float
    last_prompt: str = ""
    active: dict[str, ToolCall] = field(default_factory=dict)
    agent_stack: list[str] = field(default_factory=list)  # active Agent keys, most-recent last
    recent: deque[ToolCall] = field(default_factory=lambda: deque(maxlen=MAX_RECENT_PER_SESSION))
    cwd: str = ""

    @property
    def idle_for(self) -> float:
        return time.time() - self.last_event_at


# ---------------------------------------------------------------------------
# Event interpretation
# ---------------------------------------------------------------------------

def short(s: str, n: int = 60) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        desc = tool_input.get("description") or tool_input.get("command", "")
        return short(desc, 70)
    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        return short(tool_input.get("file_path", ""), 70)
    if tool_name == "Agent" or tool_name == "Task":
        st = tool_input.get("subagent_type") or "claude"
        d = tool_input.get("description") or ""
        return f"[{st}] {short(d, 60)}"
    if tool_name in ("Grep", "Glob"):
        return short(tool_input.get("pattern", ""), 70)
    if tool_name in ("WebFetch", "WebSearch"):
        return short(tool_input.get("url") or tool_input.get("query", ""), 70)
    # fallback: pick first short-ish string field
    for k in ("description", "query", "pattern", "command", "name"):
        v = tool_input.get(k)
        if isinstance(v, str):
            return short(v, 70)
    return ""


def tool_key(payload: dict[str, Any]) -> str:
    """Stable id for matching PreToolUse → PostToolUse."""
    for k in ("tool_use_id", "id", "tool_id"):
        v = payload.get(k)
        if v:
            return str(v)
    # fall back to (tool_name + first-input-hash); fragile but workable
    name = payload.get("tool_name", "")
    inp = payload.get("tool_input", {})
    try:
        s = json.dumps(inp, sort_keys=True, ensure_ascii=False)[:200]
    except Exception:
        s = str(inp)[:200]
    return f"{name}::{s}"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class Dashboard:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.total_events = 0
        self.events_since_start = 0
        self.started_at = time.time()

    def ingest(self, record: dict[str, Any]) -> None:
        self.total_events += 1
        self.events_since_start += 1
        ts = float(record.get("ts") or time.time())
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            return
        sid = payload.get("session_id") or "unknown"
        event = payload.get("hook_event_name", "")

        sess = self.sessions.get(sid)
        if sess is None:
            sess = Session(session_id=sid, started_at=ts, last_event_at=ts,
                           cwd=payload.get("cwd", ""))
            self.sessions[sid] = sess
        sess.last_event_at = ts
        if payload.get("cwd"):
            sess.cwd = payload["cwd"]

        if event == "UserPromptSubmit":
            sess.last_prompt = short(payload.get("prompt", ""), 100)
        elif event == "PreToolUse":
            tn = payload.get("tool_name", "?")
            desc = describe_tool(tn, payload.get("tool_input", {}))
            k = tool_key(payload)
            parent = sess.agent_stack[-1] if sess.agent_stack else None
            tc = ToolCall(key=k, session_id=sid, tool_name=tn,
                          description=desc, started_at=ts, parent_key=parent)
            sess.active[k] = tc
            if tc.is_agent:
                sess.agent_stack.append(k)
        elif event == "PostToolUse":
            k = tool_key(payload)
            tc = sess.active.pop(k, None)
            if tc is None:
                tn = payload.get("tool_name", "?")
                tc = ToolCall(key=k, session_id=sid, tool_name=tn,
                              description=describe_tool(tn, payload.get("tool_input", {})),
                              started_at=ts)
            tc.finished_at = ts
            resp = payload.get("tool_response") or payload.get("tool_result") or {}
            if isinstance(resp, dict) and (resp.get("is_error") or resp.get("error")):
                tc.error = True
            if tc.is_agent and k in sess.agent_stack:
                sess.agent_stack.remove(k)
            sess.recent.appendleft(tc)
        elif event == "SubagentStop":
            # Subagent finished — close any of its active children (no Post will come)
            if sess.agent_stack:
                parent_key = sess.agent_stack[-1]
                for k in list(sess.active.keys()):
                    tc = sess.active[k]
                    if tc.parent_key == parent_key:
                        sess.active.pop(k)
                        tc.finished_at = ts
                        sess.recent.appendleft(tc)
        elif event == "Stop":
            for k in list(sess.active.keys()):
                tc = sess.active.pop(k)
                tc.finished_at = ts
                sess.recent.appendleft(tc)
            sess.agent_stack.clear()

    def prune(self) -> None:
        now = time.time()
        dead = [sid for sid, s in self.sessions.items()
                if (now - s.last_event_at) > SESSION_DELETE_SEC]
        for sid in dead:
            self.sessions.pop(sid, None)

    # ----- rendering --------------------------------------------------------

    def render(self) -> Group:
        self.prune()
        now = time.time()
        live_sessions = [
            s for s in self.sessions.values()
            if (now - s.last_event_at) < SESSION_IDLE_HIDE_SEC or s.active
        ]
        live_sessions.sort(key=lambda s: -s.last_event_at)

        header = self._header(len(live_sessions))
        if not live_sessions:
            body = Panel(
                Align.center(Text(
                    "no recent activity. start a claude session — events will appear here.",
                    style="dim italic")),
                border_style="dim",
            )
            return Group(header, body)

        panels = [self._session_panel(s) for s in live_sessions]
        return Group(header, *panels)

    def _header(self, live_count: int) -> Panel:
        total_active = sum(len(s.active) for s in self.sessions.values())
        up = int(time.time() - self.started_at)
        t = Text()
        t.append("claude-watch  ", style="bold cyan")
        t.append(f"sessions live: ", style="dim")
        t.append(f"{live_count}", style="bold")
        t.append("   active tools: ", style="dim")
        t.append(f"{total_active}", style="bold yellow" if total_active else "bold")
        t.append(f"   events: {self.total_events}", style="dim")
        t.append(f"   uptime: {up}s", style="dim")
        return Panel(t, border_style="cyan")

    def _session_panel(self, s: Session) -> Panel:
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=10)            # status
        table.add_column(width=16, no_wrap=True)  # tool name (with indent)
        table.add_column(ratio=1, no_wrap=True)   # description
        table.add_column(justify="right", width=10)

        def add_row(status_text, tool_text, desc_text, elapsed_text):
            table.add_row(status_text, tool_text, desc_text, elapsed_text)

        # build parent → children map (only for currently-active calls)
        actives = sorted(s.active.values(), key=lambda t: t.started_at)
        children_of: dict[str, list[ToolCall]] = {}
        top_level: list[ToolCall] = []
        for tc in actives:
            if tc.parent_key and tc.parent_key in s.active:
                children_of.setdefault(tc.parent_key, []).append(tc)
            else:
                top_level.append(tc)

        if top_level:
            shown = top_level[:MAX_ACTIVE_PER_SESSION]
            hidden = len(top_level) - len(shown)
            for tc in shown:
                add_row(
                    Text("● RUN", style="bold yellow"),
                    Text(tc.tool_name, style="bold"),
                    Text(tc.description or "—"),
                    Text(f"{tc.elapsed:0.1f}s", style="yellow"),
                )
                # render children (subagent's tool calls)
                kids = children_of.get(tc.key, [])
                kid_shown = kids[:MAX_CHILDREN_PER_AGENT]
                for child in kid_shown:
                    add_row(
                        Text(" ", style="dim"),
                        Text(f"  ↳ {child.tool_name}", style="cyan"),
                        Text(child.description or "—", style="cyan"),
                        Text(f"{child.elapsed:0.1f}s", style="dim cyan"),
                    )
                if len(kids) > MAX_CHILDREN_PER_AGENT:
                    extra = len(kids) - MAX_CHILDREN_PER_AGENT
                    add_row(
                        Text(""),
                        Text(f"  … +{extra} more", style="dim italic"),
                        Text(""), Text(""),
                    )
            if hidden > 0:
                add_row(
                    Text("…", style="dim"),
                    Text(f"+{hidden} more active", style="dim italic yellow"),
                    Text(""), Text(""),
                )
        else:
            add_row(Text("idle", style="dim"), Text(""), Text(""), Text(""))

        if s.recent:
            add_row(Text("recent:", style="dim italic"), Text(""), Text(""), Text(""))
            shown_recent = list(s.recent)[:MAX_RECENT_PER_SESSION]
            for tc in shown_recent:
                status = "✓" if not tc.error else "✗"
                style = "green" if not tc.error else "red"
                add_row(
                    Text(f"{status}", style=style),
                    Text(tc.tool_name, style="dim"),
                    Text(tc.description or "—", style="dim"),
                    Text(f"{tc.elapsed:0.1f}s", style="dim"),
                )

        # title
        sid_short = s.session_id[:8] if s.session_id else "?"
        idle = s.idle_for
        idle_str = f"{int(idle)}s ago" if idle < 90 else f"{int(idle/60)}m ago"
        title = Text()
        title.append(f"session {sid_short}", style="bold")
        title.append(f"  last event {idle_str}", style="dim")
        if s.cwd:
            title.append(f"  · {os.path.basename(s.cwd)}", style="dim cyan")

        sub: list[Any] = [table]
        if s.last_prompt:
            sub.insert(0, Text(f'» {s.last_prompt}', style="italic blue"))

        border = "yellow" if s.active else "dim"
        return Panel(Group(*sub), title=title, border_style=border, title_align="left")


# ---------------------------------------------------------------------------
# Tailing
# ---------------------------------------------------------------------------

def tail_jsonl(path: Path, dashboard: Dashboard) -> None:
    """Generator-ish: open file, seek to end-ish, follow new lines.

    On first start, replay the last N lines so the dashboard isn't empty.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()

    # replay last 200 lines for initial state
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace").splitlines()[-200:]
        for line in tail:
            try:
                dashboard.ingest(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass

    f = path.open("r", encoding="utf-8", errors="replace")
    f.seek(0, os.SEEK_END)

    yield f  # hand back to caller for use in main loop


def main() -> int:
    console = Console()
    dashboard = Dashboard()

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    tail = tail_jsonl(LOG_FILE, dashboard)
    f = next(tail)

    last_inode = LOG_FILE.stat().st_ino if LOG_FILE.exists() else None

    with Live(dashboard.render(), console=console, refresh_per_second=REFRESH_HZ,
              screen=True, transient=False) as live:
        while True:
            # read whatever is new
            line = f.readline()
            while line:
                try:
                    dashboard.ingest(json.loads(line))
                except Exception:
                    pass
                line = f.readline()

            # handle log rotation / truncation
            try:
                cur = LOG_FILE.stat()
                if last_inode is None or cur.st_ino != last_inode:
                    f.close()
                    f = LOG_FILE.open("r", encoding="utf-8", errors="replace")
                    last_inode = cur.st_ino
            except FileNotFoundError:
                pass

            live.update(dashboard.render())
            time.sleep(1.0 / REFRESH_HZ)


if __name__ == "__main__":
    sys.exit(main())
