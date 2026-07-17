"""Tests for the T3 config-drift reconciler's pure parts (tools/dokploy_config_drift.py).

The live halves (Dokploy API, git-at-tag deployer loading) run in
config-drift-report.yml with --self-check; here we lock the pure logic a
silent regression would hide behind: report formatting must surface DRIFT and
ERROR loudly (a "0 drift" that silently skipped N services is the lie this
tool exists to avoid), and contents_at_ref must distinguish missing paths.
"""

from __future__ import annotations

import subprocess

import tools.dokploy_config_drift as drift
from tools.dokploy_config_drift import (
    DeployedIdentity,
    Row,
    contents_at_ref,
    format_report,
    strict_blockers,
)


def test_format_report_all_in_sync_says_green() -> None:
    rows = [Row(service="platform/postgres", verdict="in_sync")]

    report = format_report("v1.1.19", rows)

    assert "in sync 1 · DRIFT 0" in report
    assert "✅ every comparable service matches release v1.1.19." in report


def test_format_report_surfaces_drift_with_both_hashes() -> None:
    rows = [
        Row(
            service="platform/redis",
            verdict="DRIFT",
            expected="abc123",
            deployed="def456",
        ),
        Row(service="platform/postgres", verdict="in_sync"),
    ]

    report = format_report("v1.1.19", rows)

    assert "🔴 DRIFT platform/redis" in report
    assert "expected=abc123" in report and "deployed=def456" in report
    assert "✅" not in report  # a drifted run must not read as healthy


def test_format_report_surfaces_errors_loudly() -> None:
    """A service the tool could not check must appear as ERROR, never be
    silently folded into a healthy-looking summary."""
    rows = [
        Row(
            service="platform/authentik", verdict="error", note="deployer import failed"
        ),
        Row(service="platform/postgres", verdict="in_sync"),
    ]

    report = format_report("v1.1.19", rows)

    assert "⚠️ ERROR platform/authentik" in report
    assert "deployer import failed" in report
    assert "error 1" in report


def test_format_report_classifies_non_comparable_rows() -> None:
    rows = [
        Row(service="platform/portal", verdict="not_deployed"),
        Row(service="platform/signoz", verdict="structural", note="compose renamed"),
        Row(service="platform/minio", verdict="env_unavailable"),
    ]

    report = format_report("v1.1.19", rows)

    assert "not deployed: platform/portal" in report
    assert "structural: platform/signoz (compose renamed)" in report
    assert "env-skip: platform/minio" in report


def test_contents_at_ref_reads_tracked_file_and_omits_missing() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    out = contents_at_ref(head, ["pyproject.toml", "does/not/exist.txt"])

    assert b'name = "infra2"' in out["pyproject.toml"]
    assert "does/not/exist.txt" not in out  # missing at ref -> omitted, caller detects


def test_contents_at_ref_empty_paths_is_noop() -> None:
    assert contents_at_ref("HEAD", []) == {}


def _stub_single_service_scan(monkeypatch, identity, hashes_by_ref):
    class DummyDeployer:
        pass

    monkeypatch.setattr(
        drift.service_registry, "all_services", lambda: ["platform/example"]
    )
    monkeypatch.setattr(drift, "_load_deployer", lambda _sid: DummyDeployer)
    monkeypatch.setattr(
        drift, "_deployed_identities", lambda: {"platform/example": identity}
    )
    monkeypatch.setattr(drift, "_commit_at_ref", lambda _tag: "a" * 40)
    monkeypatch.setattr(drift, "_source_env_vars", lambda _dep: {"ENV": "production"})
    monkeypatch.setattr(
        drift,
        "expected_hash_at",
        lambda _dep, _context, ref, _env: (hashes_by_ref[ref], []),
    )
    return drift.scan("v1.2.3")[0]


def test_scan_accepts_older_deploy_ref_when_source_identity_is_unchanged(
    monkeypatch,
) -> None:
    old_ref = "b" * 40
    row = _stub_single_service_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="runtime",
            source_hash="v1:same",
            deploy_ref=old_ref,
        ),
        {"v1.2.3": "same", old_ref: "same"},
    )

    assert row.verdict == "in_sync"
    assert row.deployed_ref == old_ref


def test_scan_detects_source_identity_that_does_not_match_its_own_ref(
    monkeypatch,
) -> None:
    old_ref = "b" * 40
    row = _stub_single_service_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="runtime",
            source_hash="v1:current",
            deploy_ref=old_ref,
        ),
        {"v1.2.3": "current", old_ref: "different"},
    )

    assert row.verdict == "DRIFT"
    assert "does not match its deployed ref" in row.note


def test_scan_classifies_pre_migration_identity_without_false_drift(
    monkeypatch,
) -> None:
    row = _stub_single_service_scan(
        monkeypatch,
        DeployedIdentity(runtime_hash="same"),
        {"v1.2.3": "same"},
    )

    assert row.verdict == "legacy_identity"
    assert not strict_blockers([row])


def test_strict_blockers_fail_on_detector_and_structural_errors() -> None:
    rows = [
        Row("platform/a", "in_sync"),
        Row("platform/b", "DRIFT"),
        Row("platform/c", "error"),
        Row("platform/d", "structural"),
        Row("platform/e", "legacy_identity"),
    ]

    assert [row.service for row in strict_blockers(rows)] == [
        "platform/b",
        "platform/c",
        "platform/d",
    ]
