"""Tests for deploy-queue stuck detection (Dokploy-API observability layer)."""

from libs.deploy_queue import (
    ComposeDeployments,
    build_deploy_guard_alert_payload,
    deployment_start_epoch,
    find_stuck_deploys,
    is_running,
    parse_epoch_seconds,
)

NOW = 1_000_000.0  # fixed epoch for deterministic ages


def _compose(compose_id, name, deployments, service_id="platform/alerting"):
    return ComposeDeployments(
        compose_id=compose_id,
        compose_name=name,
        service_id=service_id,
        environment="production",
        deployments=tuple(deployments),
    )


def test_parse_epoch_seconds_variants():
    assert parse_epoch_seconds(1_000_000) == 1_000_000.0
    # millisecond epoch is detected and scaled down
    assert parse_epoch_seconds(1_700_000_000_000) == 1_700_000_000.0
    # ISO-8601 with Z
    assert parse_epoch_seconds("1970-01-01T00:00:00Z") == 0.0
    # naive ISO is treated as UTC
    assert parse_epoch_seconds("1970-01-01T00:00:01") == 1.0
    # unparseable / empty / bool -> None (so the deploy is skipped, not mis-aged)
    assert parse_epoch_seconds("not-a-date") is None
    assert parse_epoch_seconds("") is None
    assert parse_epoch_seconds(None) is None
    assert parse_epoch_seconds(True) is None


def test_deployment_start_epoch_prefers_startedAt_then_createdAt():
    assert deployment_start_epoch({"startedAt": 5, "createdAt": 9}) == 5.0
    assert deployment_start_epoch({"createdAt": 9}) == 9.0
    assert deployment_start_epoch({"updatedAt": 3}) == 3.0
    assert deployment_start_epoch({"finishedAt": 1}) is None


def test_is_running_is_case_insensitive():
    assert is_running({"status": "running"})
    assert is_running({"status": "RUNNING"})
    assert not is_running({"status": "done"})
    assert not is_running({"status": "error"})
    assert not is_running({})


def test_find_stuck_only_flags_running_past_ceiling():
    composes = [
        # running 600s, ceiling 300 -> STUCK
        _compose(
            "c1",
            "platform/app",
            [{"status": "running", "deploymentId": "d1", "startedAt": NOW - 600}],
        ),
        # running only 100s -> not stuck
        _compose(
            "c2",
            "platform/redis",
            [{"status": "running", "deploymentId": "d2", "startedAt": NOW - 100}],
        ),
        # done long ago -> never stuck
        _compose(
            "c3",
            "platform/db",
            [{"status": "done", "deploymentId": "d3", "startedAt": NOW - 9999}],
        ),
    ]
    stuck = find_stuck_deploys(composes, NOW, ceiling_seconds=300)
    assert [s.compose_id for s in stuck] == ["c1"]
    assert stuck[0].deployment_id == "d1"
    assert stuck[0].age_seconds == 600


def test_find_stuck_reports_oldest_running_per_compose():
    composes = [
        _compose(
            "c1",
            "platform/app",
            [
                {"status": "running", "deploymentId": "new", "startedAt": NOW - 400},
                {"status": "running", "deploymentId": "old", "startedAt": NOW - 1200},
            ],
        )
    ]
    stuck = find_stuck_deploys(composes, NOW, ceiling_seconds=300)
    assert len(stuck) == 1
    # the oldest (queue-blocking) one is reported
    assert stuck[0].deployment_id == "old"
    assert stuck[0].age_seconds == 1200


def test_find_stuck_skips_deploys_without_parseable_start():
    composes = [
        _compose(
            "c1",
            "x",
            [{"status": "running", "deploymentId": "d", "startedAt": "garbage"}],
        )
    ]
    assert find_stuck_deploys(composes, NOW, ceiling_seconds=0) == []


def test_alert_payload_shape_matches_bridge_contract():
    composes = [
        _compose(
            "c1",
            "platform/app",
            [{"status": "running", "deploymentId": "d1", "startedAt": NOW - 600}],
        )
    ]
    stuck = find_stuck_deploys(composes, NOW, ceiling_seconds=300)
    payload = build_deploy_guard_alert_payload(stuck)
    # keys the bridge's format_signoz_alert reads
    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "DeployQueueStuck"
    assert payload["commonLabels"]["severity"] == "critical"
    assert isinstance(payload["alerts"], list) and len(payload["alerts"]) == 1
    alert = payload["alerts"][0]
    assert alert["labels"]["service_id"] == "platform/alerting"
    assert alert["labels"]["environment"] == "production"
    assert alert["labels"]["identity_schema"] == "v1"
    assert "stuck running" in alert["annotations"]["summary"]


def test_alert_payload_resolved_when_empty():
    payload = build_deploy_guard_alert_payload([], firing=False)
    assert payload["status"] == "resolved"
    assert payload["alerts"] == []
