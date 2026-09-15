"""The runner surfaces the deployer's verdict lines on a green sync (#702 review)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _sync_runner(monkeypatch):
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location(
        "sync_runner_for_verdict", ROOT / "bootstrap/06.iac_runner/sync_runner.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_runner_for_verdict"] = module
    spec.loader.exec_module(module)
    return module


STDOUT = """
╭──────────────────────╮
│ postgres sync        │
╰──────────────────────╯
ℹ️  Local config hash: 1a2b3c
✅ postgres: secret supply ok (2 keys)
Deploying compose abc...
✅ Deployed postgres (composeId: abc)
✅ postgres: in service — 2 service(s) running and healthy
✅ postgres: deployed with hash 1a2b3c
"""


def test_verdict_lines_are_the_console_verdicts_only(monkeypatch):
    runner = _sync_runner(monkeypatch)
    assert runner.verdict_lines(STDOUT) == [
        "✅ postgres: secret supply ok (2 keys)",
        "✅ Deployed postgres (composeId: abc)",
        "✅ postgres: in service — 2 service(s) running and healthy",
        "✅ postgres: deployed with hash 1a2b3c",
    ]
    assert runner.verdict_lines("no markers\nat all\n") == []
    many = "\n".join(f"✅ line {i}" for i in range(30))
    assert len(runner.verdict_lines(many)) == runner.MAX_VERDICT_LINES
    assert runner.verdict_lines(many)[-1] == "✅ line 29"


def test_a_green_result_carries_its_verdict_in_both_dict_shapes(monkeypatch):
    runner = _sync_runner(monkeypatch)
    result = runner.ServiceSyncResult(
        service="platform/postgres", task="postgres.sync", success=True, stdout=STDOUT
    )
    public = result.to_public_dict()
    assert public["success"] and "diagnostic" not in public
    assert public["verdict"][-2] == (
        "✅ postgres: in service — 2 service(s) running and healthy"
    )
    assert result.to_dict()["verdict"] == public["verdict"]
    failed = runner.ServiceSyncResult(
        service="platform/postgres",
        task="postgres.sync",
        success=False,
        stdout="❌ postgres: not in service after deploy: no container for postgres",
        stderr="",
    )
    assert failed.to_public_dict()["verdict"] == [
        "❌ postgres: not in service after deploy: no container for postgres"
    ]
    assert "diagnostic" in failed.to_public_dict()
