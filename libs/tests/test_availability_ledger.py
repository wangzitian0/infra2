"""Positive + negative tests for the availability ledger (foundational).

This is infrastructure that proves "the service was up", so it gets paired
positive (正例) and negative (反例) cases: the negatives exist to prove the code
*refuses* to report a down signal as healthy and *refuses* to trust junk input.
"""

from __future__ import annotations

from datetime import UTC, datetime

from libs.availability_ledger import (
    apply_outages,
    build_report_message,
    summarize_ledger,
)

# Two days: dokploy stayed perfect (96/96), minio dropped 2 (94/96).
HEALTHY_AND_DEGRADED = {
    "as_of": "2026-06-10",
    "window_days": 7,
    "ledger": [
        {
            "date": "2026-06-10",
            "runs": 48,
            "signals": {
                "production:dokploy-public-route": {
                    "ok": 48,
                    "fail": 0,
                    "severity": "critical",
                },
                "production:minio-public-route": {
                    "ok": 46,
                    "fail": 2,
                    "severity": "warning",
                    "lastDomain": "network",
                },
            },
        },
        {
            "date": "2026-06-09",
            "runs": 48,
            "signals": {
                "production:dokploy-public-route": {
                    "ok": 48,
                    "fail": 0,
                    "severity": "critical",
                },
                "production:minio-public-route": {
                    "ok": 48,
                    "fail": 0,
                    "severity": "warning",
                },
            },
        },
    ],
}


# ---- 正例 (positive): correct aggregation -----------------------------------


def test_positive_multi_day_aggregation_is_exact() -> None:
    summary = summarize_ledger(HEALTHY_AND_DEGRADED)

    assert summary["total_runs"] == 96
    assert summary["signal_count"] == 2
    assert summary["perfect_count"] == 1  # only dokploy
    assert summary["overall_uptime_pct"] == round((190 / 192) * 100, 3)

    by_id = {s["id"]: s for s in summary["signals"]}
    assert by_id["production:dokploy-public-route"]["uptime_pct"] == 100.0
    assert by_id["production:minio-public-route"]["uptime_pct"] == round(
        (94 / 96) * 100, 3
    )


def test_positive_all_perfect_window_reports_clean() -> None:
    ledger = {
        "as_of": "2026-06-10",
        "window_days": 1,
        "ledger": [
            {"date": "2026-06-10", "runs": 48, "signals": {"a": {"ok": 48, "fail": 0}}}
        ],
    }
    summary = summarize_ledger(ledger)
    assert summary["perfect_count"] == 1
    assert "held 100% availability" in build_report_message(summary)


# ---- 反例 (negative): must NOT pass when it shouldn't -----------------------


def test_negative_degraded_signal_is_never_reported_perfect() -> None:
    """The core guarantee: any failure must drop the signal below 100%."""
    summary = summarize_ledger(HEALTHY_AND_DEGRADED)
    minio = next(
        s for s in summary["signals"] if s["id"].endswith("minio-public-route")
    )

    assert minio["fail"] == 2
    assert minio["uptime_pct"] < 100.0
    assert minio not in [s for s in summary["signals"] if s["fail"] == 0]
    # Worst signal sorts first and is surfaced in the report, not hidden.
    assert summary["signals"][0]["id"] == minio["id"]
    assert "minio-public-route" in build_report_message(summary)
    assert "[network]" in build_report_message(summary)


def test_negative_malformed_input_does_not_inflate_or_crash() -> None:
    """Junk days/signals are ignored; they cannot raise availability."""
    ledger = {
        "as_of": "2026-06-10",
        "ledger": [
            "not-a-day",  # wrong type
            {"date": "d1", "runs": 10, "signals": "not-a-map"},  # bad signals
            {"date": "d2", "runs": 10, "signals": {"x": "not-a-map"}},  # bad counts
            {
                "date": "d3",
                "runs": 10,
                "signals": {"x": {"ok": 8, "fail": 2}},
            },  # the only real one
        ],
    }
    summary = summarize_ledger(ledger)

    assert summary["signal_count"] == 1  # only "x" counted
    assert summary["signals"][0]["ok"] == 8
    assert summary["signals"][0]["fail"] == 2
    assert summary["overall_uptime_pct"] == 80.0  # 8/10, junk ignored


def test_negative_junk_counts_are_coerced_to_zero() -> None:
    ledger = {
        "ledger": [
            {"date": "d", "runs": "x", "signals": {"s": {"ok": "abc", "fail": -5}}}
        ]
    }
    summary = summarize_ledger(ledger)
    assert summary["total_runs"] == 0
    assert summary["signals"][0]["ok"] == 0
    assert summary["signals"][0]["fail"] == 0


def test_negative_zero_checks_does_not_divide_by_zero() -> None:
    empty = summarize_ledger({"ledger": []})
    assert empty["signal_count"] == 0
    assert empty["overall_uptime_pct"] == 100.0  # nothing to disprove
    assert "held 100% availability" in build_report_message(empty)


# ---- #904: the VPS cannot count its own outage; the Worker's edges do ----------

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
MS = 1000


def _ms(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 9, day, hour, minute, tzinfo=UTC).timestamp() * MS)


def _vps_ledger() -> dict:
    """Two days of a runner that saw everything healthy (it could not see its outage)."""
    signals = {
        "production:vault-http": {"ok": 1380, "fail": 0},
        "production:minio-public-route": {"ok": 1380, "fail": 0},
        "staging:vault-http": {"ok": 1440, "fail": 0},
    }
    return {
        "as_of": "2026-09-24",
        "ledger": [
            {"date": "2026-09-24", "runs": 700, "signals": dict(signals)},
            {"date": "2026-09-23", "runs": 1380, "signals": dict(signals)},
        ],
    }


def _apply(outages: list) -> dict:
    return apply_outages(
        _vps_ledger(),
        outages,
        environment="production",
        check_interval_seconds=60,
        now=NOW,
    )


def _signal(ledger: dict, date: str, sid: str) -> dict:
    return next(day for day in ledger["ledger"] if day["date"] == date)["signals"][sid]


def test_positive_an_outage_is_a_failure_of_every_production_signal() -> None:
    applied = _apply(
        [{"environment": "production", "start": _ms(23, 10), "end": _ms(23, 11, 30)}]
    )
    for sid in ("production:vault-http", "production:minio-public-route"):
        assert _signal(applied, "2026-09-23", sid)["fail"] == 90
        assert _signal(applied, "2026-09-24", sid)["fail"] == 0
    # staging was not what went down
    assert _signal(applied, "2026-09-23", "staging:vault-http")["fail"] == 0
    assert applied["outages"] == {
        "environment": "production",
        "count": 1,
        "minutes": 90.0,
        "unreadable": 0,
    }


def test_negative_a_runner_perfect_through_its_own_outage_is_not_reported_perfect() -> (
    None
):
    """The reason the Worker keeps the edges: without them this reads 100%."""
    before = summarize_ledger(_vps_ledger())
    assert before["perfect_count"] == before["signal_count"]

    applied = _apply(
        [{"environment": "production", "start": _ms(23, 10), "end": _ms(23, 10, 30)}]
    )
    summary = summarize_ledger(applied)
    assert summary["overall_uptime_pct"] < 100.0
    assert summary["perfect_count"] == 1  # only staging
    message = build_report_message(summary)
    assert (
        "production VPS unreachable (Cloudflare heartbeat edges): 1 outage(s), 30.0 min"
        in message
    )
    assert "production:vault-http" in message


def test_an_outage_across_midnight_splits_by_day() -> None:
    applied = _apply(
        [{"environment": "production", "start": _ms(23, 23, 0), "end": _ms(24, 0, 45)}]
    )
    assert _signal(applied, "2026-09-23", "production:vault-http")["fail"] == 60
    assert _signal(applied, "2026-09-24", "production:vault-http")["fail"] == 45


def test_an_open_outage_counts_until_now() -> None:
    applied = _apply(
        [{"environment": "production", "start": _ms(24, 11, 0), "end": None}]
    )
    assert _signal(applied, "2026-09-24", "production:vault-http")["fail"] == 60


def test_an_outage_before_the_window_is_clipped_to_it() -> None:
    applied = _apply(
        [{"environment": "production", "start": _ms(20, 0), "end": _ms(23, 0, 10)}]
    )
    assert _signal(applied, "2026-09-23", "production:vault-http")["fail"] == 10
    assert {day["date"] for day in applied["ledger"]} == {"2026-09-23", "2026-09-24"}


def test_a_whole_day_the_runner_never_recorded_is_added_as_failures() -> None:
    ledger = _vps_ledger()
    ledger["ledger"] = [
        day for day in ledger["ledger"] if day["date"] == "2026-09-23"
    ]  # the runner was down all of today so far
    applied = apply_outages(
        ledger,
        [{"environment": "production", "start": _ms(24, 0), "end": None}],
        environment="production",
        check_interval_seconds=60,
        now=NOW,
    )
    assert _signal(applied, "2026-09-24", "production:minio-public-route") == {
        "ok": 0,
        "fail": 720,
        "lastDomain": "vps-unreachable",
    }


def test_other_environments_and_unreadable_records_are_not_counted_but_shown() -> None:
    applied = _apply(
        [
            {"environment": "staging", "start": _ms(23, 1), "end": _ms(23, 2)},
            {"environment": "production", "start": "yesterday", "end": None},
            "garbage",
        ]
    )
    assert _signal(applied, "2026-09-23", "production:vault-http")["fail"] == 0
    assert applied["outages"]["unreadable"] == 2
    message = build_report_message(summarize_ledger(applied))
    assert "2 unreadable outage record(s) NOT counted" in message
