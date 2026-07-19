"""Tests for tools/no_new_wheels_lint.py (#542 task 5)."""

from __future__ import annotations

from pathlib import Path

from tools.no_new_wheels_lint import (
    lint_python_callsites,
    lint_scheduled_jobs,
    registered_signal_names,
)


def test_current_tree_passes() -> None:
    signals = registered_signal_names()
    assert lint_python_callsites(signals=signals) == []
    assert lint_scheduled_jobs(signals=signals) == []


def test_synthetic_unregistered_callsite_is_caught(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "libs").mkdir()
    rogue = tmp_path / "tools" / "rogue_watcher.py"
    rogue.write_text(
        "from tools.out_of_band_watchdog import deliver_out_of_band_alert\n"
        "deliver_out_of_band_alert({}, 'boom')\n"
    )
    errors = lint_python_callsites(root=tmp_path, signals={"registered-signal"})
    assert len(errors) == 1
    assert "rogue_watcher.py" in errors[0]
    assert "no-new-wheels" in errors[0]


def test_synthetic_bogus_signal_name_is_caught(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "libs").mkdir()
    rogue = tmp_path / "tools" / "rogue.py"
    rogue.write_text(
        "# alerts-as: not-a-real-signal\n"
        "from libs.alerting import deliver_infra2_report\n"
        "deliver_infra2_report('x')\n"
    )
    errors = lint_python_callsites(root=tmp_path, signals={"real-signal"})
    assert len(errors) == 1
    assert "not-a-real-signal" in errors[0]


def test_exempt_marker_with_reason_passes(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "libs").mkdir()
    ok = tmp_path / "tools" / "engine.py"
    ok.write_text(
        "# alert-delivery-exempt: the delivery engine itself\n"
        "from libs.infra_probes import post_alert_bridge_payload\n"
        "post_alert_bridge_payload({})\n"
    )
    assert lint_python_callsites(root=tmp_path, signals=set()) == []
