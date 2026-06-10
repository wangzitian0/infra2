"""Positive + negative tests for the availability ledger (foundational).

This is infrastructure that proves "the service was up", so it gets paired
positive (正例) and negative (反例) cases: the negatives exist to prove the code
*refuses* to report a down signal as healthy and *refuses* to trust junk input.
"""

from __future__ import annotations

from libs.availability_ledger import build_report_message, summarize_ledger

# Two days: dokploy stayed perfect (96/96), minio dropped 2 (94/96).
HEALTHY_AND_DEGRADED = {
    "as_of": "2026-06-10",
    "window_days": 7,
    "ledger": [
        {
            "date": "2026-06-10",
            "runs": 48,
            "signals": {
                "production:dokploy-public-route": {"ok": 48, "fail": 0, "severity": "critical"},
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
                "production:dokploy-public-route": {"ok": 48, "fail": 0, "severity": "critical"},
                "production:minio-public-route": {"ok": 48, "fail": 0, "severity": "warning"},
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
    assert by_id["production:minio-public-route"]["uptime_pct"] == round((94 / 96) * 100, 3)


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
    minio = next(s for s in summary["signals"] if s["id"].endswith("minio-public-route"))

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
            {"date": "d3", "runs": 10, "signals": {"x": {"ok": 8, "fail": 2}}},  # the only real one
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
