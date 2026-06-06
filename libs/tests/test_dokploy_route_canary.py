"""Tests for the Dokploy dynamic route canary."""

from __future__ import annotations

import subprocess
from pathlib import Path

from libs.dokploy_route_canary import (
    RouteCanaryConfig,
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
        self.updated = False
        self.deployed = False

    def find_compose_by_name(self, *_args, **_kwargs) -> dict | None:
        return None

    def create_compose(self, *_args, **_kwargs) -> dict:
        return {"composeId": self.compose_id}

    def update_compose(self, *_args, **_kwargs) -> dict:
        self.updated = True
        return {"ok": True}

    def deploy_compose(self, _compose_id: str) -> dict:
        self.deployed = True
        return {"message": "Deployment queued"}

    def get_compose(self, _compose_id: str) -> dict:
        index = min(self.get_calls, len(self.deployments) - 1)
        self.get_calls += 1
        return {
            "composeStatus": "done",
            "deployments": self.deployments[index],
        }


def test_canary_compose_uses_same_host_web_and_api_routes() -> None:
    """Infra-011.9: canary mirrors same-host web/API Traefik routing."""
    config = RouteCanaryConfig(
        host="route-canary.example.com",
        environment_id="env-1",
        compose_name="route-canary-test",
    )

    compose = render_canary_compose(config)

    assert "container_name: route-canary-test-web" in compose
    assert "container_name: route-canary-test-api" in compose
    assert "Host(`route-canary.example.com`)" in compose
    assert "PathPrefix(`/api`)" in compose
    assert "priority=100" in compose
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


def test_canary_passes_after_deployment_containers_and_public_routes() -> None:
    """Infra-011.9: canary proves Dokploy, Docker labels, and public routes."""
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
        compose_name="route-canary-test",
        ssh_host="vps.example.com",
        timeout_seconds=15,
        interval_seconds=5,
    )

    def fake_command(_command: str, _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=(
                "route-canary-test-web Up\n"
                "route-canary-test-api Up\n"
                "/route-canary-test-web {\"traefik.http.routers.route-canary-test-web.rule\":\"ok\"}"
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


def test_canary_workflow_is_manual_and_fast_failing() -> None:
    """Infra-011.9: operators can run the platform canary without app CI."""
    workflow = (ROOT / ".github/workflows/dokploy-route-canary.yml").read_text()

    assert "workflow_dispatch" in workflow
    assert "schedule:" in workflow
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "timeout-minutes: 8" in workflow
    assert "DOKPLOY_API_KEY secret is required" in workflow
    assert "No SSH key configured; canary will skip Docker container/label inspection" in workflow
    assert "python tools/dokploy_route_canary.py" in workflow
    assert "GITHUB_STEP_SUMMARY" in workflow
    assert "--environment-id=\"$environment_id\"" in workflow
    assert "--dokploy-host \"cloud.zitian.party\"" in workflow


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
