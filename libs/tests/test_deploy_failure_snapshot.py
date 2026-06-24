"""#768: platform-health snapshot emitted on a failed fixed-compose deploy."""

from __future__ import annotations

from tools import deploy_failure_snapshot as dfs


class _FakeClient:
    def __init__(self, compose=None, raise_exc=None):
        self._compose = compose
        self._raise = raise_exc

    def get_compose(self, compose_id):
        if self._raise is not None:
            raise self._raise
        return self._compose


def test_classify_dokploy_deployment_error():
    assert dfs.classify("error", []) == "dokploy-deployment-error"
    assert (
        dfs.classify("running", [{"status": "error", "startedAt": "2"}])
        == "dokploy-deployment-error"
    )


def test_classify_no_deployment_record():
    assert dfs.classify("idle", []) == "no-deployment-record"
    assert dfs.classify("running", []) == "no-deployment-record"


def test_classify_platform_ok_means_app_failure():
    # Dokploy says the rollout is healthy -> the failure is above the platform.
    assert (
        dfs.classify("running", [{"status": "done", "startedAt": "1"}])
        == "platform-ok-app-failure"
    )


def test_build_snapshot_surfaces_latest_deployment_error():
    client = _FakeClient(
        compose={
            "composeStatus": "error",
            "deployments": [
                {
                    "status": "done",
                    "startedAt": "2026-01-01T00:00:00Z",
                    "errorMessage": "",
                },
                {
                    "status": "error",
                    "startedAt": "2026-01-02T00:00:00Z",
                    "errorMessage": "image pull failed",
                },
            ],
        }
    )
    snap = dfs.build_snapshot(client, "compose-1")
    assert snap["compose_id"] == "compose-1"
    assert snap["compose_status"] == "error"
    assert snap["deployment_count"] == 2
    # picks the latest by startedAt, not list order
    assert snap["latest_deployment_status"] == "error"
    assert snap["latest_deployment_error"] == "image pull failed"
    assert snap["platform_failure_domain"] == "dokploy-deployment-error"


def test_build_snapshot_is_failsafe_on_api_error():
    snap = dfs.build_snapshot(_FakeClient(raise_exc=RuntimeError("boom")), "compose-1")
    assert snap["platform_failure_domain"] == "dokploy-api-unreachable"
    assert "RuntimeError" in snap["error"]


def test_emit_writes_markdown_to_summary(tmp_path):
    summary = tmp_path / "summary.md"
    client = _FakeClient(
        compose={
            "composeStatus": "error",
            "deployments": [
                {"status": "error", "startedAt": "1", "errorMessage": "rollout failed"}
            ],
        }
    )
    snap = dfs.emit_failure_snapshot(client, "compose-1", summary_path=str(summary))
    text = summary.read_text(encoding="utf-8")
    assert "Platform-health snapshot (deploy failure)" in text
    assert "dokploy-deployment-error" in text
    assert "rollout failed" in text
    assert snap["compose_id"] == "compose-1"


def test_emit_never_raises_even_if_client_explodes(tmp_path):
    summary = tmp_path / "summary.md"
    # A diagnostic must never mask the original deploy error.
    snap = dfs.emit_failure_snapshot(
        _FakeClient(raise_exc=RuntimeError("boom")),
        "compose-1",
        summary_path=str(summary),
    )
    assert snap["compose_id"] == "compose-1"
    assert "dokploy-api-unreachable" in summary.read_text(encoding="utf-8")
