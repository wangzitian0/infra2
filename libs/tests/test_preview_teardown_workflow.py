"""Contract: infra2 owns the event-driven 1:1 PR-preview teardown."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "preview-teardown.yml"


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_teardown_receives_app_dispatch_on_pr_close() -> None:
    # YAML parses `on:` as the boolean True key.
    triggers = _load()[True]
    assert triggers["repository_dispatch"]["types"] == ["preview-teardown"]
    # also manually runnable with a pr_number
    assert "pr_number" in triggers["workflow_dispatch"]["inputs"]


def test_teardown_runs_deploy_v2_down_through_the_front_door() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")
    # The authoritative teardown goes through deploy_v2 (the app never touches Dokploy).
    assert "tools.deploy_v2" in body
    assert "--type preview/pr" in body
    assert "--down" in body
    # PR number comes from the dispatch payload, validated as a positive integer.
    assert "client_payload.pr_number" in body
    assert "=~ ^[1-9][0-9]*$" in body


def test_teardown_alerts_on_failure() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")
    # A failed teardown means a leak is imminent -> out-of-band alert.
    assert "deliver_out_of_band_alert" in body
    assert "if: failure()" in body
