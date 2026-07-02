"""Tests for the T3 config-drift reconciler's pure parts (tools/dokploy_config_drift.py).

The live halves (Dokploy API, git-at-tag deployer loading) run in
config-drift-report.yml with --self-check; here we lock the pure logic a
silent regression would hide behind: report formatting must surface DRIFT and
ERROR loudly (a "0 drift" that silently skipped N services is the lie this
tool exists to avoid), and contents_at_ref must distinguish missing paths.
"""

from __future__ import annotations

import subprocess

from tools.dokploy_config_drift import Row, contents_at_ref, format_report


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
