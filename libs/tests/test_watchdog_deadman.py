"""Worker cron completion reaches an independent external dead-man switch."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "cloudflare/infra-watchdog/worker.js"
HARNESS = ROOT / "libs/tests/fixtures/watchdog_deadman_harness.mjs"
DEPLOY_WORKFLOW = ROOT / ".github/workflows/deploy-cloudflare-watchdog.yml"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scheduler_success_failure_and_missing_secret(tmp_path: Path) -> None:
    module = tmp_path / "worker.mjs"
    module.write_text(WORKER.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
        ["node", str(HARNESS), str(module)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    cases = json.loads(result.stdout)
    url = "https://hc-ping.com/test-worker-check"
    assert cases["success"]["ok"]
    assert url in cases["success"]["calls"]
    assert f"{url}/fail" not in cases["success"]["calls"]
    assert not cases["workerFailure"]["ok"]
    assert f"{url}/fail" in cases["workerFailure"]["calls"]
    assert not cases["missingUrl"]["ok"]
    assert url not in cases["missingUrl"]["error"]


def test_worker_production_deploy_requires_exact_head_manual_dispatch() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    trigger = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "approved_sha:" in trigger
    assert "push:" not in trigger
    assert 'test "${GITHUB_ACTOR}" = "wangzitian0"' in workflow
    assert 'test "${GITHUB_REF}" = "refs/heads/main"' in workflow
    assert 'test "${GITHUB_SHA}" = "${APPROVED_SHA}"' in workflow
