#!/usr/bin/env python3
"""Claude Code hook that keeps the workspace orchestrator able to hear events.

A background-task or Monitor notification reaches the main conversation only between
tool calls, never during one, so a single long foreground wait leaves the orchestrator
deaf for its whole length (harness/workspace/coordination.md, Orchestrator liveness).

- PreToolUse (matcher "Bash"), main conversation only: deny a foreground call whose
  timeout exceeds 4 minutes, and any foreground wait loop (``until``/``while`` ...
  ``sleep``, ``gh run watch``, ``gh pr checks --watch``). Those belong in the
  background or under Monitor.
- Stop, main conversation only: when ``<scratchpad_dir>/sweep/watch.json`` lists items
  and no ``tools.harness sweep <that file> --watch`` process is alive, block the stop
  once and ask for the watch to be armed. ``stop_hook_active`` makes it a one-time
  reminder, never a loop.

Subagents (the hook input carries ``agent_id``) are exempt: they run long verification
inline and finish their deliverable before ending their turn.

Standard library only, so it runs under any ``python3``. Not wired by this repository:
the owner adds it to ``.claude/settings.json`` (snippet in coordination.md). Exit 2
blocks the call or the stop and shows stderr to the model; exit 0 allows.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

MAX_BLOCK_MS = 240_000
ALLOW, BLOCK = 0, 2
WAIT_LOOP = re.compile(r"\b(until|while)\b.*?\bdo\b.*?\bsleep\b", re.S)
GH_WATCH = re.compile(r"\bgh\s+(run\s+watch\b|pr\s+checks\b[^|;&]*--watch\b)")
WATCH_FILE = Path("sweep") / "watch.json"
SWEEP_COMMAND = "python -m tools.harness sweep"


def running_commands() -> list[str]:
    out = subprocess.run(
        ["ps", "-Ao", "args="], capture_output=True, text=True, check=False
    ).stdout
    return out.splitlines()


def watch_armed(watch_path: str, commands: list[str]) -> bool:
    return any("sweep" in c and "--watch" in c and watch_path in c for c in commands)


def _pre_tool_use(tool: dict) -> tuple[int, str]:
    if tool.get("run_in_background"):
        return ALLOW, ""
    command = str(tool.get("command") or "")
    try:
        timeout = int(tool.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0  # an unreadable timeout gets the tool's own default
    if timeout > MAX_BLOCK_MS:
        return BLOCK, (
            f"orchestrator liveness: a foreground call may block at most "
            f"{MAX_BLOCK_MS // 60000} min (asked {timeout // 1000}s). Use "
            f"run_in_background, or Monitor with `{SWEEP_COMMAND} <watch.json> --watch`, "
            "so events can still reach you."
        )
    if WAIT_LOOP.search(command) or GH_WATCH.search(command):
        return BLOCK, (
            "orchestrator liveness: foreground wait loops are not allowed in the main "
            "conversation. Add the item to <scratchpad>/sweep/watch.json and arm a "
            f"persistent Monitor with `{SWEEP_COMMAND} <abs path>/watch.json --watch` "
            "(judge exit codes, never grep)."
        )
    return ALLOW, ""


def _stop(payload: dict, commands: Callable[[], list[str]]) -> tuple[int, str]:
    scratch = payload.get("scratchpad_dir")
    if not scratch:
        return ALLOW, ""
    watch_path = str(Path(scratch) / WATCH_FILE)
    try:
        data = json.loads(Path(watch_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ALLOW, ""
    # The same two shapes `tools.harness sweep` accepts: {"items": [...]} or a bare list.
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    if items and not watch_armed(watch_path, commands()):
        return BLOCK, (
            f"orchestrator liveness: {len(items)} watched item(s) in {watch_path} but "
            "no sweep watch is running. Arm a persistent Monitor with "
            f"`{SWEEP_COMMAND} {watch_path} --watch`, or empty the list, before "
            "ending the turn."
        )
    return ALLOW, ""


def decide(
    payload: dict, commands: Callable[[], list[str]] = running_commands
) -> tuple[int, str]:
    if payload.get("agent_id"):
        return ALLOW, ""
    event = payload.get("hook_event_name")
    if event == "PreToolUse" and payload.get("tool_name") == "Bash":
        return _pre_tool_use(payload.get("tool_input") or {})
    if event == "Stop" and not payload.get("stop_hook_active"):
        return _stop(payload, commands)
    return ALLOW, ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return ALLOW  # never break the session on a malformed payload
    if not isinstance(payload, dict):
        return ALLOW
    code, message = decide(payload)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
