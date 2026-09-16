"""tools/orchestrator_guard_hook: the main conversation never blocks for long, never
ends its turn with an unwatched watch list; subagents are exempt."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import orchestrator_guard_hook as hook

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "orchestrator_guard_hook.py"
# The 2026-09-16 waiter: it grepped the gate's prose and never matched the real outcome.
INCIDENT = (
    'until OUT=$(uv run python tools/merge_ready.py 901 2>&1 | tail -1); echo "$OUT" | '
    'grep -qE "clear to merge|High|Medium|fail|red|DIRTY"; do sleep 30; done'
)


def _bash(command, timeout=None, background=False, **extra):
    tool = {"command": command, "description": "x", "run_in_background": background}
    if timeout is not None:
        tool["timeout"] = timeout
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": tool,
        **extra,
    }


def test_the_incident_loop_is_denied_in_the_main_conversation():
    assert hook.decide(_bash(INCIDENT, timeout=600_000))[0] == hook.BLOCK
    code, message = hook.decide(_bash(INCIDENT))
    assert code == hook.BLOCK and "python -m tools.harness sweep" in message


def test_the_same_loop_is_allowed_in_the_background_and_in_subagents():
    assert hook.decide(_bash(INCIDENT, background=True))[0] == hook.ALLOW
    assert hook.decide(_bash(INCIDENT, timeout=600_000, agent_id="a1"))[0] == hook.ALLOW


def test_a_long_timeout_is_denied_and_a_short_one_allowed():
    code, message = hook.decide(_bash("pytest -q", timeout=600_000))
    assert code == hook.BLOCK and "asked 600s" in message
    assert hook.decide(_bash("pytest -q", timeout=240_000))[0] == hook.ALLOW
    assert hook.decide(_bash("pytest -q"))[0] == hook.ALLOW
    assert hook.decide(_bash("pytest -q", timeout="soon"))[0] == hook.ALLOW


def test_gh_watchers_are_denied_and_gh_reads_allowed():
    assert hook.decide(_bash("gh run watch 123 -R o/r"))[0] == hook.BLOCK
    assert (
        hook.decide(_bash("gh pr checks 9 -R o/r --watch --fail-fast"))[0] == hook.BLOCK
    )
    assert (
        hook.decide(_bash("gh pr checks 9 -R o/r --json name,bucket"))[0] == hook.ALLOW
    )


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "while read -r l; do echo $l; done < f",
        "sleep_count=3; echo $sleep_count",
    ],
)
def test_ordinary_commands_pass(command):
    assert hook.decide(_bash(command))[0] == hook.ALLOW


def test_other_tools_and_events_pass():
    assert hook.decide({"hook_event_name": "PreToolUse", "tool_name": "Read"})[0] == 0
    assert hook.decide({"hook_event_name": "PostToolUse", "tool_name": "Bash"})[0] == 0


@pytest.fixture
def watch_list(tmp_path):
    path = tmp_path / "sweep" / "watch.json"
    path.parent.mkdir()
    return path


def _stop(scratch, commands, **extra):
    payload = {"hook_event_name": "Stop", "scratchpad_dir": str(scratch), **extra}
    return hook.decide(payload, commands=lambda: commands)


def test_a_watch_list_without_a_watch_blocks_the_stop_once(tmp_path, watch_list):
    watch_list.write_text(json.dumps({"items": [{"kind": "pr"}]}))
    code, message = _stop(tmp_path, [])
    assert code == hook.BLOCK and str(watch_list) in message
    assert _stop(tmp_path, [], stop_hook_active=True)[0] == hook.ALLOW
    assert _stop(tmp_path, [], agent_id="a1")[0] == hook.ALLOW


def test_an_armed_watch_or_an_empty_list_allows_the_stop(tmp_path, watch_list):
    watch_list.write_text(json.dumps({"items": [{"kind": "pr"}]}))
    armed = f"uv run python -m tools.harness sweep {watch_list} --watch"
    assert _stop(tmp_path, [armed])[0] == hook.ALLOW
    other = "uv run python -m tools.harness sweep other.json --watch"
    assert _stop(tmp_path, [other])[0] == hook.BLOCK
    one_shot = f"uv run python -m tools.harness sweep {watch_list}"
    assert _stop(tmp_path, [one_shot])[0] == hook.BLOCK
    watch_list.write_text(json.dumps({"items": []}))
    assert _stop(tmp_path, [])[0] == hook.ALLOW


@pytest.mark.parametrize("content", [None, "{not json", "[1, 2]"])
def test_a_missing_or_broken_list_allows_the_stop(tmp_path, watch_list, content):
    if content is not None:
        watch_list.write_text(content)
    assert _stop(tmp_path, [])[0] == hook.ALLOW


def test_a_stop_without_a_scratchpad_is_allowed():
    assert hook.decide({"hook_event_name": "Stop"})[0] == hook.ALLOW


def test_running_commands_lists_this_process():
    assert any("pytest" in line or "python" in line for line in hook.running_commands())


def test_main_reads_the_payload_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_bash(INCIDENT))))
    assert hook.main() == hook.BLOCK
    assert "orchestrator liveness" in capsys.readouterr().err
    for garbage in ("nope", "[1]"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(garbage))
        assert hook.main() == hook.ALLOW
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_bash("git status"))))
    assert hook.main() == hook.ALLOW
    assert capsys.readouterr().err == ""


def test_the_script_honours_the_hook_stdin_contract():
    denied = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(_bash(INCIDENT)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert denied.returncode == hook.BLOCK
    assert "orchestrator liveness" in denied.stderr
    garbage = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="nope",
        capture_output=True,
        text=True,
        check=False,
    )
    assert garbage.returncode == hook.ALLOW
