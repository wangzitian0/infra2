"""Tests for the Dokploy dynamic route canary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from libs.dokploy_route_canary import (
    RouteCanaryConfig,
    _http_get,
    render_github_summary,
    render_canary_compose,
    run_route_canary,
)


ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDokploy:
    def __init__(self, deployments: list[list[dict]] | None = None) -> None:
        self.compose_id = "cmp-canary"
        self.deployments = deployments or [[]]
        self.get_calls = 0
        self.source_type = "raw"
        self.compose_status = "done"
        self.updated = False
        self.update_calls: list[dict] = []
        self.deployed = False
        self.redeployed = False

    def find_compose_by_name(self, *_args, **_kwargs) -> dict | None:
        return None

    def create_compose(self, *_args, **kwargs) -> dict:
        self.source_type = kwargs.get("source_type") or self.source_type
        return {"composeId": self.compose_id}

    def update_compose(self, compose_id, compose_file=None, env=None, source_type=None):
        self.updated = True
        if source_type:
            self.source_type = source_type
        self.update_calls.append(
            {
                "compose_id": compose_id,
                "compose_file": compose_file,
                "env": env,
                "source_type": source_type,
            }
        )
        return {"ok": True}

    def deploy_compose(self, _compose_id: str) -> dict:
        self.deployed = True
        return {"message": "Deployment queued"}

    def redeploy_compose(self, _compose_id: str) -> dict:
        self.redeployed = True
        return {"message": "Redeployment queued"}

    def get_compose(self, _compose_id: str) -> dict:
        index = min(self.get_calls, len(self.deployments) - 1)
        self.get_calls += 1
        return {
            "composeStatus": self.compose_status,
            "composeType": "docker-compose",
            "sourceType": self.source_type,
            "appName": "dokploy-route-canary",
            "deployments": self.deployments[index],
        }


class FakeDokployDeploymentApi(FakeDokploy):
    def __init__(
        self,
        *,
        compose_deployments: list[dict],
        deployment_api_snapshots: list[list[dict]],
    ) -> None:
        super().__init__(deployments=[compose_deployments])
        self.deployment_api_snapshots = deployment_api_snapshots
        self.deployment_api_calls = 0

    def get_compose_deployments(self, _compose_id: str) -> list[dict]:
        index = min(self.deployment_api_calls, len(self.deployment_api_snapshots) - 1)
        self.deployment_api_calls += 1
        return self.deployment_api_snapshots[index]


class FakeRepairableDokploy(FakeDokployDeploymentApi):
    def __init__(self) -> None:
        super().__init__(
            compose_deployments=[{"deploymentId": "old", "status": "done"}],
            deployment_api_snapshots=[
                [{"deploymentId": "old", "status": "done"}],
                [{"deploymentId": "old", "status": "done"}],
                [{"deploymentId": "old", "status": "done"}],
                [{"deploymentId": "old", "status": "done"}],
                [],
                [{"deploymentId": "new", "status": "done", "createdAt": "2026-01-01"}],
            ],
        )
        self.create_calls = 0
        self.deleted: list[tuple[str, bool]] = []
        self.source_type = "github"

    def find_compose_by_name(self, *_args, **_kwargs) -> dict | None:
        return {"composeId": "cmp-stale", "name": "dokploy-route-canary"}

    def create_compose(self, *_args, **_kwargs) -> dict:
        self.create_calls += 1
        self.source_type = "github"
        return {"composeId": "cmp-recreated"}

    def delete_compose(self, compose_id: str, *, delete_volumes: bool = False) -> dict:
        self.deleted.append((compose_id, delete_volumes))
        return {}


def test_canary_compose_uses_same_host_web_and_api_routes() -> None:
    """Infra-011.9: canary mirrors same-host web/API Traefik routing."""
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        compose_name="route-canary-test",
        nonce="nonce-1",
    )

    compose = render_canary_compose(config)

    assert "container_name: route-canary-test-web" in compose
    assert "container_name: route-canary-test-api" in compose
    assert "Host(`route-canary.example.com`)" in compose
    assert "PathPrefix(`/api`)" in compose
    assert "priority=100" in compose
    assert "infra2.route-canary.nonce=nonce-1" in compose
    assert "dokploy-network" in compose


def test_canary_fails_fast_when_deploy_record_never_changes() -> None:
    """Infra-011.9: accepted deploy without a new record is a platform failure."""
    clock = FakeClock()
    client = FakeDokploy(deployments=[[{"deploymentId": "old", "status": "done"}]])
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        timeout_seconds=15,
        interval_seconds=5,
    )

    report = run_route_canary(
        config,
        client,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "fail"
    assert report.failure_domain == "dokploy-worker-or-deployment-record"
    assert report.steps[-1].name == "deployment-record"
    assert "did not produce a new running/done deployment" in report.steps[-1].detail
    assert client.redeployed is True


def test_canary_retries_redeploy_when_initial_deploy_has_no_record() -> None:
    """Infra-011.9: compose.redeploy is the fallback for Dokploy deploy no-ops."""
    clock = FakeClock()
    client = FakeDokploy(
        deployments=[
            [],
            [],
            [],
            [],
            [{"deploymentId": "new", "status": "done", "createdAt": "2026-01-01"}],
        ]
    )
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        timeout_seconds=5,
        interval_seconds=5,
    )

    report = run_route_canary(
        config,
        client,
        http_get=lambda _url, _timeout: (404, "not found"),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert client.deployed is True
    assert client.redeployed is True
    assert client.updated is True
    assert report.failure_domain == "traefik-public-route"
    assert [step.name for step in report.steps] == [
        "compose-upsert",
        "deploy-trigger",
        "deployment-record",
        "redeploy-trigger",
        "deployment-record",
        "public-routes",
    ]


def test_canary_accepts_running_record_but_still_requires_public_routes() -> None:
    """Infra-011.9: running deployment records prove work, not route health."""
    clock = FakeClock()
    client = FakeDokploy(
        deployments=[
            [],
            [{"deploymentId": "new", "status": "running", "createdAt": "2026-01-01"}],
        ]
    )
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        timeout_seconds=15,
        interval_seconds=5,
    )

    report = run_route_canary(
        config,
        client,
        http_get=lambda _url, _timeout: (404, "not found"),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "fail"
    assert report.failure_domain == "traefik-public-route"
    assert report.steps[2].data["latest_status"] == "running"
    assert report.steps[-1].name == "public-routes"


def test_canary_normalizes_created_compose_to_raw_source_type() -> None:
    """Infra-011.9: new run-scoped compose must not keep Dokploy's github default."""
    clock = FakeClock()
    client = FakeDokploy(
        deployments=[
            [],
            [{"deploymentId": "new", "status": "done", "createdAt": "2026-01-01"}],
        ]
    )
    captured = {}

    def capture_update(compose_id, compose_file=None, env=None, source_type=None):
        captured["compose_id"] = compose_id
        captured["compose_file"] = compose_file
        captured["env"] = env
        captured["source_type"] = source_type
        return FakeDokploy.update_compose(
            client,
            compose_id,
            compose_file=compose_file,
            env=env,
            source_type=source_type,
        )

    client.update_compose = capture_update

    report = run_route_canary(
        RouteCanaryConfig(
            host="route-canary.example.com",
            environment_id="env-1",
            timeout_seconds=15,
            interval_seconds=5,
        ),
        client,
        http_get=lambda _url, _timeout: (200, "ok"),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "pass"
    assert captured["compose_id"] == client.compose_id
    assert captured["source_type"] == "raw"
    assert "container_name: dokploy-route-canary-web" in captured["compose_file"]
    assert "CANARY_HOST=route-canary.example.com" in captured["env"]


def test_canary_uses_deployment_api_when_compose_snapshot_is_stale() -> None:
    """Infra-011.9: deployment proof reads Dokploy deployment API before compose snapshots."""
    clock = FakeClock()
    client = FakeDokployDeploymentApi(
        compose_deployments=[{"deploymentId": "old", "status": "done"}],
        deployment_api_snapshots=[
            [{"deploymentId": "old", "status": "done"}],
            [
                {"deploymentId": "old", "status": "done"},
                {"id": "new", "status": "done", "createdAt": "2026-01-01"},
            ],
        ],
    )
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        timeout_seconds=10,
        interval_seconds=5,
    )

    report = run_route_canary(
        config,
        client,
        http_get=lambda _url, _timeout: (200, "ok"),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "pass"
    assert client.redeployed is False
    assert report.steps[2].name == "deployment-record"
    assert report.steps[2].data["new_deployment_ids"] == ["new"]


def test_canary_repairs_guarded_stale_compose_after_deploy_noop() -> None:
    """Infra-011.9: guarded canary repair recreates stale compose state."""
    clock = FakeClock()
    client = FakeRepairableDokploy()
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        compose_name="dokploy-route-canary",
        timeout_seconds=0,
        interval_seconds=1,
        repair_stale_compose=True,
    )

    report = run_route_canary(
        config,
        client,
        http_get=lambda _url, _timeout: (200, "ok"),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "pass"
    assert report.compose_id == "cmp-recreated"
    assert client.deleted == [("cmp-stale", False)]
    assert client.create_calls == 1
    assert client.update_calls[-1]["compose_id"] == "cmp-recreated"
    assert client.update_calls[-1]["source_type"] == "raw"
    assert [step.name for step in report.steps] == [
        "compose-upsert",
        "deploy-trigger",
        "deployment-record",
        "redeploy-trigger",
        "deployment-record",
        "compose-recreate",
        "repair-deploy-trigger",
        "deployment-record",
        "public-routes",
    ]


def test_canary_fails_fast_with_source_type_evidence_for_github_compose() -> None:
    """Infra-011.9: provider drift is classified before opaque worker polling."""
    clock = FakeClock()
    client = FakeDokploy(
        deployments=[
            [
                {
                    "deploymentId": "old",
                    "status": "error",
                    "logPath": "/etc/dokploy/logs/canary/error.log",
                    "createdAt": "2026-01-01",
                    "errorMessage": "Github Provider not found",
                }
            ]
        ]
    )
    client.source_type = "github"
    client.compose_status = "error"

    def create_keeps_github(*_args, **_kwargs):
        return {"composeId": client.compose_id}

    def update_keeps_github(compose_id, compose_file=None, env=None, source_type=None):
        client.updated = True
        client.update_calls.append(
            {
                "compose_id": compose_id,
                "compose_file": compose_file,
                "env": env,
                "source_type": source_type,
            }
        )
        return {"ok": True}

    client.create_compose = create_keeps_github
    client.update_compose = update_keeps_github
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        timeout_seconds=15,
        interval_seconds=5,
    )

    report = run_route_canary(
        config,
        client,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "fail"
    assert report.failure_domain == "dokploy-compose-source-type"
    assert report.steps[2].name == "deployment-record"
    assert report.steps[2].data["sourceType"] == "github"
    assert report.steps[2].data["composeStatus"] == "error"
    assert report.steps[2].data["latest_deployment_status"] == "error"
    assert report.steps[2].data["latest_deployment_logPath"].endswith("error.log")
    summary = render_github_summary(report)
    assert "dokploy-compose-source-type" in summary
    assert "sourceType" in summary
    assert "latest_deployment_logPath" in summary


def test_canary_repair_refuses_non_canary_assets() -> None:
    """Infra-011.9: stale repair must not delete arbitrary Dokploy composes."""
    clock = FakeClock()
    client = FakeRepairableDokploy()
    config = RouteCanaryConfig(
        host="app.example.com",
        environment_id="env-1",
        compose_name="app",
        timeout_seconds=0,
        interval_seconds=1,
        repair_stale_compose=True,
    )

    report = run_route_canary(
        config,
        client,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "fail"
    assert report.failure_domain == "dokploy-control-plane"
    assert report.steps[-1].name == "compose-recreate"
    assert "restricted to route-canary" in report.steps[-1].detail
    assert client.deleted == []


def test_canary_classifies_public_route_failure_after_deployment() -> None:
    """Infra-011.9: deployment success with persistent 404 isolates Traefik/public route."""
    clock = FakeClock()
    client = FakeDokploy(
        deployments=[
            [{"deploymentId": "old", "status": "done"}],
            [
                {"deploymentId": "old", "status": "done"},
                {"deploymentId": "new", "status": "done", "createdAt": "2026-01-01"},
            ],
        ]
    )
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        timeout_seconds=15,
        interval_seconds=5,
    )

    report = run_route_canary(
        config,
        client,
        http_get=lambda _url, _timeout: (404, "not found"),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "fail"
    assert report.failure_domain == "traefik-public-route"
    assert report.steps[-1].name == "public-routes"
    assert report.steps[-1].data["web_status"] == 404
    assert report.steps[-1].data["api_status"] == 404


def test_canary_public_route_timeout_becomes_probe_evidence() -> None:
    """Infra-011.9: public route read timeouts must not crash before summary output."""
    with patch(
        "libs.dokploy_route_canary.urlopen",
        side_effect=TimeoutError("The read operation timed out"),
    ):
        status, body = _http_get(
            "https://route-canary.example.com/",
            timeout=0.1,
            http_get=None,
        )

    assert status == 0
    assert "timed out" in body


def test_canary_passes_after_deployment_containers_and_public_routes() -> None:
    """Infra-011.9: canary proves Dokploy, Docker labels, and public routes."""
    clock = FakeClock()
    client = FakeDokploy(
        deployments=[
            [],
            [{"deploymentId": "new", "status": "done", "createdAt": "2026-01-01"}],
        ]
    )
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        compose_name="route-canary-test",
        ssh_host="vps.example.com",
        timeout_seconds=15,
        interval_seconds=5,
    )

    def fake_command(
        _command: str, _timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=(
                "route-canary-test-web Up\n"
                "route-canary-test-api Up\n"
                "/route-canary-test-web "
                '{"traefik.http.routers.route-canary-test-web.rule":'
                '"Host(`route-canary.example.com`)"}\n'
                "/route-canary-test-api "
                '{"traefik.http.routers.route-canary-test-api.rule":'
                '"Host(`route-canary.example.com`) && PathPrefix(`/api`)"}'
            ),
            stderr="",
        )

    report = run_route_canary(
        config,
        client,
        http_get=lambda _url, _timeout: (200, "ok"),
        command_runner=fake_command,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "pass"
    assert report.failure_domain == ""
    assert [step.name for step in report.steps] == [
        "compose-upsert",
        "deploy-trigger",
        "deployment-record",
        "docker-containers",
        "public-routes",
    ]


def test_canary_fails_docker_phase_when_labels_are_stale() -> None:
    """Infra-011.9: stale Traefik labels must fail before public 404 polling."""
    clock = FakeClock()
    client = FakeDokploy(
        deployments=[
            [],
            [{"deploymentId": "new", "status": "done", "createdAt": "2026-01-01"}],
        ]
    )
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        compose_name="route-canary-test",
        ssh_host="vps.example.com",
        timeout_seconds=10,
        interval_seconds=5,
    )
    public_calls = []

    def fake_command(
        _command: str, _timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=(
                "route-canary-test-web Up\n"
                "route-canary-test-api Up\n"
                "/route-canary-test-web "
                '{"traefik.http.routers.route-canary-test-web.rule":'
                '"Host(`old-route-canary.example.com`)"}\n'
                "/route-canary-test-api "
                '{"traefik.http.routers.route-canary-test-api.rule":'
                '"Host(`old-route-canary.example.com`) && PathPrefix(`/api`)"}'
            ),
            stderr="",
        )

    report = run_route_canary(
        config,
        client,
        http_get=lambda url, _timeout: public_calls.append(url) or (200, "ok"),
        command_runner=fake_command,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert report.status == "fail"
    assert report.failure_domain == "docker-runtime"
    assert report.steps[-1].name == "docker-containers"
    assert "do not match the canary host" in report.steps[-1].detail
    assert public_calls == []


def test_canary_workflow_is_manual_and_fast_failing() -> None:
    """Infra-011.9: operators can run the platform canary without app CI."""
    workflow = (ROOT / ".github/workflows/dokploy-route-canary.yml").read_text()

    assert "workflow_dispatch" in workflow
    assert "schedule:" in workflow
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "timeout-minutes: 8" in workflow
    assert 'FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"' in workflow
    assert "DOKPLOY_API_KEY secret is required" in workflow
    assert "workflow_dispatch environment_id input is required" in workflow
    assert "dokploy-canary-configuration" in workflow
    assert r"Status: \`fail\`" in workflow
    assert r"Failure domain: \`dokploy-canary-configuration\`" in workflow
    assert "Status: skipped" not in workflow
    assert "skipping scheduled/push route canary" not in workflow
    assert (
        "No SSH key configured; canary will skip Docker container/label inspection"
        in workflow
    )
    assert "python tools/dokploy_route_canary.py" in workflow
    assert "GITHUB_STEP_SUMMARY" in workflow
    assert '--environment-id="$environment_id"' in workflow
    assert '--dokploy-host "cloud.zitian.party"' in workflow
    assert '--nonce "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in workflow
    assert "route-canary.zitian.party" in workflow
    assert "route-canary-${GITHUB_RUN_ID}.zitian.party" not in workflow
    assert 'default: "dokploy-route-canary"' in workflow
    assert "--repair-stale-compose" in workflow


def test_canary_github_summary_lists_failure_domain_and_phase_evidence() -> None:
    """Infra-011.9: canary failures are readable without parsing raw JSON logs."""
    clock = FakeClock()
    client = FakeDokploy(deployments=[[{"deploymentId": "old", "status": "done"}]])
    report = run_route_canary(
        RouteCanaryConfig(
            host="route-canary.example.com",
            environment_id="env-1",
            timeout_seconds=10,
            interval_seconds=5,
        ),
        client,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    summary = render_github_summary(report)

    assert "## Dokploy Route Canary" in summary
    assert "- Status: `fail`" in summary
    assert "- Failure domain: `dokploy-worker-or-deployment-record`" in summary
    assert "| compose-upsert | pass |" in summary
    assert "| deploy-trigger | pass |" in summary
    assert "| deployment-record | fail |" in summary
    assert "deployment_count" in summary


def test_route_canary_alert_failure_pages_out_of_band(monkeypatch) -> None:
    """#369: a non-pass route canary pages out-of-band (Feishu) instead of dying
    silently in the hourly workflow log (the ~23h blind spot)."""
    import libs.alerting as al
    import tools.dokploy_route_canary as rc
    from types import SimpleNamespace

    sent = {}
    monkeypatch.setattr(
        al, "deliver_out_of_band_text", lambda env, text, **kw: sent.update(text=text)
    )
    rc._alert_failure(SimpleNamespace(status="fail", failure_domain="dokploy-routing"))
    assert "route canary FAILED" in sent["text"]
    assert "dokploy-routing" in sent["text"]


def test_route_canary_alert_failure_never_raises(monkeypatch) -> None:
    """Alerting must never change the probe's exit code."""
    import libs.alerting as al
    import tools.dokploy_route_canary as rc
    from types import SimpleNamespace

    def boom(*_a, **_k):
        raise RuntimeError("feishu down")

    monkeypatch.setattr(al, "deliver_out_of_band_text", boom)
    rc._alert_failure(SimpleNamespace(status="fail", failure_domain=None))  # must not raise
