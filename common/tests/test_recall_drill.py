"""Tests for 8-class dry-run recall drill."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from common.alerting import PAGER_FIELDS
from common.observability.recall_drill import (
    load_failure_classes,
    run_recall_drill,
)

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "tools" / "recall_drill.py"


def test_load_failure_classes_matches_eight_ssot_classes() -> None:
    """Verify loading failure classes returns the 8 SSOT classes from watchdog-signals.yaml."""
    classes = load_failure_classes()
    expected_classes = {
        "host-reachability": "cloudflare",
        "alert-pipeline": "cloudflare",
        "watchdog-liveness": "github",
        "service-health": "vps",
        "data-protection": "github",
        "config-drift": "github",
        "peer-scheduler": "github",
        "scheduled-job-start": "github",
    }
    assert classes == expected_classes


def test_run_recall_drill_dry_run_verifies_all_classes() -> None:
    """Run recall drill in dry-run mode and verify all 8 classes pass."""
    report = run_recall_drill(dry_run=True)

    assert report.status == "pass"
    assert report.dry_run is True
    assert report.total_classes == 8
    assert len(report.results) == 8

    expected_classes = load_failure_classes()
    for class_name, expected_layer in expected_classes.items():
        assert class_name in report.results
        res = report.results[class_name]
        assert res["expected_layer"] == expected_layer
        assert res["actual_layer"] == expected_layer
        assert res["layer_verified"] is True
        assert res["fields_verified"] is True
        assert res["status"] == "pass"

        payload = res["payload"]
        for field in PAGER_FIELDS:
            if field != "日志":
                assert field in payload, f"missing field {field} in {class_name}"
                assert payload[field] != ""


def test_run_recall_drill_refuses_non_dry_run() -> None:
    """Refuse recall drill when dry_run is False to protect ambient environments."""
    with pytest.raises(ValueError, match="Recall drill only supports dry_run=True"):
        run_recall_drill(dry_run=False)


def test_recall_drill_cli_dry_run_exits_zero() -> None:
    """Execute recall drill CLI with default dry-run flag and assert exit code zero."""
    result = subprocess.run(
        [sys.executable, str(CLI), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "pass"' in result.stdout
    assert '"total_classes": 8' in result.stdout


def test_recall_drill_cli_summary_reports_verified_counts() -> None:
    """Verify CLI prints accurate passed class counts in text summary output."""
    res_success = subprocess.run(
        [sys.executable, str(CLI)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_success.returncode == 0
    assert "Recall drill: pass (8/8 classes verified)" in res_success.stdout

    fail_script = (
        "import sys, tools.recall_drill as cli, common.observability.recall_drill as rd; "
        "rd.SIMULATED_FAILURES['host-reachability']['signal_id'] = 'invalid.signal.id'; "
        "sys.exit(cli.main())"
    )
    res_fail = subprocess.run(
        [sys.executable, "-c", fail_script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_fail.returncode == 2
    assert "Recall drill: fail (7/8 classes verified)" in res_fail.stdout


def test_recall_drill_cli_refuses_no_dry_run() -> None:
    """Execute recall drill CLI with --no-dry-run flag and assert exit code one."""
    result = subprocess.run(
        [sys.executable, str(CLI), "--no-dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Recall drill only supports dry_run=True" in result.stderr


def test_recall_drill_fails_on_signal_layer_mismatch() -> None:
    """Fail recall drill when signal layer mismatches failure class pager."""
    from common.observability.watchdog_audit import _load_inventory

    inv = _load_inventory()
    mutated_signals = []
    for sig in inv["signals"]:
        if sig.get("signal_id") == "production.dokploy.public-route":
            mutated_sig = dict(sig)
            mutated_sig["primary_owner"] = "github"
            mutated_signals.append(mutated_sig)
        else:
            mutated_signals.append(sig)
    inv["signals"] = mutated_signals

    report = run_recall_drill(dry_run=True, inventory=inv)
    assert report.status == "fail"
    res = report.results["host-reachability"]
    assert res["layer_verified"] is False
    assert res["status"] == "fail"
    assert res["actual_layer"] == "github"
    assert res["expected_layer"] == "cloudflare"


def test_recall_drill_fails_on_signal_failure_class_mismatch() -> None:
    """Fail recall drill when signal failure class mismatches."""
    from common.observability.watchdog_audit import _load_inventory

    inv = _load_inventory()
    mutated_signals = []
    for sig in inv["signals"]:
        if sig.get("signal_id") == "production.dokploy.public-route":
            mutated_sig = dict(sig)
            mutated_sig["failure_class"] = "service-health"
            mutated_signals.append(mutated_sig)
        else:
            mutated_signals.append(sig)
    inv["signals"] = mutated_signals

    report = run_recall_drill(dry_run=True, inventory=inv)
    assert report.status == "fail"
    res = report.results["host-reachability"]
    assert res["layer_verified"] is False
    assert res["status"] == "fail"


def test_recall_drill_fails_on_unsimulated_failure_class() -> None:
    """Fail recall drill when inventory has an unsimulated failure class."""
    from common.observability.watchdog_audit import _load_inventory

    inv = _load_inventory()
    classes = dict(inv.get("failure_classes", {}))
    classes["unsimulated-new-class"] = {"pager": "vps", "covers": "test"}
    inv["failure_classes"] = classes

    report = run_recall_drill(dry_run=True, inventory=inv)
    assert report.status == "fail"
    res = report.results["unsimulated-new-class"]
    assert res["status"] == "fail"
    assert res["layer_verified"] is False
