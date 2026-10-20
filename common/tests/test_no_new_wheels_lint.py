"""Tests for tools/no_new_wheels_lint.py (#542 task 5, #720 debt expiration)."""

from __future__ import annotations

import datetime
from pathlib import Path

from common.core.no_new_wheels_lint import (
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
    (tmp_path / "common").mkdir()
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
    (tmp_path / "common").mkdir()
    rogue = tmp_path / "tools" / "rogue.py"
    rogue.write_text(
        "# alerts-as: not-a-real-signal\n"
        "from common.alerting import deliver_infra2_report\n"
        "deliver_infra2_report('x')\n"
    )
    errors = lint_python_callsites(root=tmp_path, signals={"real-signal"})
    assert len(errors) == 1
    assert "not-a-real-signal" in errors[0]


def test_callsite_inside_a_domain_package_is_caught(tmp_path: Path) -> None:
    """#846: the scan was `glob("*.py")`, so it never descended into common/<domain>/.
    Moving an implementation into a domain package therefore took it out of the lint's
    reach silently — the lint stayed green because it had stopped looking, not because
    the file complied. This is the case that used to slip through."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "common" / "observability").mkdir(parents=True)
    rogue = tmp_path / "common" / "observability" / "rogue.py"
    rogue.write_text(
        "from common.observability.probes import post_alert_bridge_payload\n"
        "post_alert_bridge_payload({})\n"
    )
    errors = lint_python_callsites(root=tmp_path, signals={"registered-signal"})
    assert len(errors) == 1
    assert "common/observability/rogue.py" in errors[0]
    assert "no-new-wheels" in errors[0]


def test_exempt_marker_with_reason_passes(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "common").mkdir()
    ok = tmp_path / "tools" / "engine.py"
    ok.write_text(
        "# alert-delivery-exempt: the delivery engine itself; review=2027-04-01\n"
        "from common.observability.probes import post_alert_bridge_payload\n"
        "post_alert_bridge_payload({})\n"
    )
    assert (
        lint_python_callsites(
            root=tmp_path, signals=set(), today=datetime.date(2026, 10, 20)
        )
        == []
    )


def test_exempt_marker_without_review_date_fails(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "common").mkdir()
    rogue = tmp_path / "tools" / "engine.py"
    rogue.write_text(
        "# alert-delivery-exempt: missing review date\n"
        "from common.observability.probes import post_alert_bridge_payload\n"
        "post_alert_bridge_payload({})\n"
    )
    errors = lint_python_callsites(
        root=tmp_path, signals=set(), today=datetime.date(2026, 10, 20)
    )
    assert len(errors) == 1
    assert "review=YYYY-MM-DD" in errors[0]


def test_expired_exempt_marker_fails(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "common").mkdir()
    expired = tmp_path / "tools" / "engine.py"
    expired.write_text(
        "# nowheel: reason=domain-semantics; review=2026-10-01\n"
        "from common.observability.probes import post_alert_bridge_payload\n"
        "post_alert_bridge_payload({})\n"
    )
    errors = lint_python_callsites(
        root=tmp_path, signals=set(), today=datetime.date(2026, 10, 20)
    )
    assert len(errors) == 1
    assert "exemption expired on 2026-10-01" in errors[0]


def test_scheduled_job_expired_nowheel_fails(tmp_path: Path) -> None:
    wf = tmp_path / "ops-checks.yml"
    wf.write_text(
        "name: ops\n"
        "jobs:\n"
        "  my-cron:\n"
        "    if: github.event_name == 'schedule' || github.event.schedule != ''\n"
        "    # nowheel: reason=temp-audit; review=2026-09-01\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    errors = lint_scheduled_jobs(
        ops_checks=wf, signals=set(), today=datetime.date(2026, 10, 20)
    )
    assert len(errors) == 1
    assert "exemption expired on 2026-09-01" in errors[0]
