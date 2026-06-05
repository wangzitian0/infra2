"""Tests for infra service probe contracts."""

from __future__ import annotations

import subprocess

from libs.infra_probes import (
    build_probe_alert_payload,
    failed_results,
    parse_probe_specs,
    run_probe,
)


def test_probe_specs_parse_http_tcp_and_command() -> None:
    """#183: probe config is code-owned and typed."""
    specs = parse_probe_specs(
        """
        vault-http|http|https://vault.example/v1/sys/health|200,429|critical|3
        postgres|tcp|platform-postgres:5432|connected|critical|5
        docker|command|docker info|Server|warning|10
        """
    )

    assert [spec.kind for spec in specs] == ["http", "tcp", "command"]
    assert specs[0].expected == "200,429"
    assert specs[2].timeout_seconds == 10


def test_http_probe_matches_expected_status() -> None:
    spec = parse_probe_specs("minio|http|https://minio.example/minio/health/live|200")[0]

    result = run_probe(spec, http_get=lambda _url, _timeout: (200, "ok"))

    assert result.ok is True
    assert result.observed == "200:ok"


def test_tcp_and_command_probe_failures_are_classified() -> None:
    tcp = parse_probe_specs("redis|tcp|platform-redis:6379|connected")[0]
    tcp_result = run_probe(
        tcp,
        tcp_connect=lambda *_args: (_ for _ in ()).throw(OSError("refused")),
    )

    command = parse_probe_specs("docker|command|docker info|Server")[0]
    command_result = run_probe(
        command,
        command_runner=lambda *_args: subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="daemon down",
        ),
    )

    assert failed_results([tcp_result, command_result]) == [tcp_result, command_result]
    assert "refused" in tcp_result.summary
    assert "daemon down" in command_result.summary


def test_failed_probes_build_signoz_compatible_payload() -> None:
    spec = parse_probe_specs("vault|http|https://vault.example/v1/sys/health|200")[0]
    result = run_probe(spec, http_get=lambda *_args: (503, "sealed"))

    payload = build_probe_alert_payload([result])

    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "InfraServiceProbeFailed"
    assert payload["alerts"][0]["labels"]["service"] == "vault"
    assert payload["alerts"][0]["annotations"]["observed"] == "503:sealed"

