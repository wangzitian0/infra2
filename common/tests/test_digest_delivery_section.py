"""The "Delivery" section of the weekly digest, from 40 stage records (#1645)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


import common.observability.digest as digest
from common.observability.delivery_section import (
    delivery_lines,
    percentile,
    summarize_delivery,
    wilson_interval,
)
from common.deploy import stage_record_reader as stage_records
from tools import watchdog_weekly_digest

STAGING, PROD, PREVIEW = (
    "staging.release_validation",
    "prod.release_integrity",
    "preview.runtime",
)


def _rec(env, phase, run, units, seconds, failed=(), passed=("ok",)):
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    checks = [{"name": n, "ok": False, "detail": "", "seconds": 1.0} for n in failed]
    checks += [{"name": n, "ok": True, "detail": "", "seconds": 1.0} for n in passed]
    return {
        "schema": 1,
        "env": env,
        "phase": phase,
        "subject": f"tag:v1.0.{run}",
        "run_url": f"https://github.com/o/r/actions/runs/{env}{run}",
        "started_at": start.isoformat(),
        "finished_at": (start + timedelta(seconds=seconds)).isoformat(),
        "checks": checks,
        "units": list(units),
        "verdict": "fail" if failed else "pass",
    }


def _forty_records() -> list[dict]:
    sdk = "sdk:3.29.0"
    app, infra = ("app:finance_report/app", sdk), ("infra:platform/alerting", sdk)
    records: list[dict] = []
    # staging: 6 runs. s1 s3 s4 pass; s2 fails in operation; s5 passes; s6 fails in post.
    plan = {
        1: (app, 100, 10),
        2: (app, 400, 0),
        3: (app, 200, 20),
        4: (app, 300, 30),
        5: (infra, 150, 40),
        6: (infra, 250, 50),
    }
    for run, (units, op, post) in plan.items():
        records.append(_rec(STAGING, "pre", run, units, 5))
        failed = ("deploy:x", "g1") if run == 2 else ()
        records.append(_rec(STAGING, "operation", run, units, op, failed))
        if run == 6:
            records.append(_rec(STAGING, "post", run, units, post, ("g1", "g2", "g3")))
        elif run != 2:
            records.append(_rec(STAGING, "post", run, units, post))
    # prod: 4 runs. p1 p2 p3 pass; p4 fails the pre record.
    for run, (op, post) in {1: (60, 10), 2: (80, 20), 3: (100, 30)}.items():
        units = ("app:truealpha/app", sdk)
        records += [
            _rec(PROD, "pre", run, units, 5),
            _rec(PROD, "operation", run, units, op),
            _rec(PROD, "post", run, units, post),
        ]
    records.append(_rec(PROD, "pre", 4, ("app:truealpha/app", sdk), 5, ("provenance",)))
    # preview: 13 runs with a post record; one fails.
    for run in range(13):
        failed = ("fr:preview_gate",) if run == 0 else ()
        records.append(_rec(PREVIEW, "post", run, (app[0], sdk), 5, failed))
    assert len(records) == 40
    return records


def test_wilson_interval_matches_the_published_values() -> None:
    low, high = wilson_interval(4, 6)
    assert (round(low * 100, 1), round(high * 100, 1)) == (30.0, 90.3)
    assert wilson_interval(0, 0) == (0.0, 0.0)
    assert wilson_interval(5, 5)[1] == 1.0


def test_percentile_is_nearest_rank() -> None:
    assert percentile([100, 150, 200, 250, 300, 400], 0.5) == 200
    assert percentile([100, 150, 200, 250, 300, 400], 0.9) == 400
    assert percentile([], 0.5) == 0.0


def test_forty_records_give_counts_interval_percentiles_and_top_failing_ids() -> None:
    summary = summarize_delivery(_forty_records())
    assert list(summary) == ["preview", "staging", "prod"]
    assert delivery_lines(summary) == [
        "preview: 13 runs, pass 92.3% (95% CI 66.7-98.6), operation p50 0 s p90 0 s, post p50 5 s p90 5 s, top failing: fr:preview_gate x1",
        "  app: 13 runs, pass 92.3% (95% CI 66.7-98.6), operation p50 0 s p90 0 s, post p50 5 s p90 5 s, top failing: fr:preview_gate x1",
        "  sdk: 13 runs, pass 92.3% (95% CI 66.7-98.6), operation p50 0 s p90 0 s, post p50 5 s p90 5 s, top failing: fr:preview_gate x1",
        "staging: 6 runs, pass 66.7% (95% CI 30.0-90.3), operation p50 200 s p90 400 s, post p50 30 s p90 50 s, top failing: g1 x2, deploy:x x1, g2 x1",
        "  app: 4 runs, pass 75.0% (95% CI 30.1-95.4), operation p50 200 s p90 400 s, post p50 20 s p90 30 s, top failing: deploy:x x1, g1 x1",
        "  sdk: 6 runs, pass 66.7% (95% CI 30.0-90.3), operation p50 200 s p90 400 s, post p50 30 s p90 50 s, top failing: g1 x2, deploy:x x1, g2 x1",
        "  infra: 2 runs, pass 50.0% (95% CI 9.5-90.5), operation p50 150 s p90 250 s, post p50 40 s p90 50 s, top failing: g1 x1, g2 x1, g3 x1",
        "prod: 4 runs, pass 75.0% (95% CI 30.1-95.4), operation p50 80 s p90 100 s, post p50 20 s p90 30 s, top failing: provenance x1",
        "  app: 4 runs, pass 75.0% (95% CI 30.1-95.4), operation p50 80 s p90 100 s, post p50 20 s p90 30 s, top failing: provenance x1",
        "  sdk: 4 runs, pass 75.0% (95% CI 30.1-95.4), operation p50 80 s p90 100 s, post p50 20 s p90 30 s, top failing: provenance x1",
    ]


def test_a_run_without_a_post_record_is_a_failed_run() -> None:
    only_pre = [_rec(STAGING, "pre", 1, ("app:x",), 5)]
    assert summarize_delivery(only_pre)["staging"]["all"]["passed"] == 0


def test_records_of_other_envs_are_left_out() -> None:
    assert (
        summarize_delivery([_rec("github_ci.merge_authority", "post", 1, (), 5)]) == {}
    )


# -- the weekly digest builder calls the section (the end-to-end test of #1433) -------------


def _run_cli(monkeypatch, *, fetcher):
    monkeypatch.setattr(digest, "fetch_recent_runs", lambda *a, **k: [])
    texts: list[str] = []
    cards: list[dict] = []

    def deliver(_env, text, *, card=None):
        texts.append(text)
        cards.append(card)

    rc = digest.run_digest_cli(
        {
            "GITHUB_TOKEN": "tok",
            "WATCHDOG_DIGEST_REVIEW_LOGS": "0",
            "WATCHDOG_DIGEST_ESTATE_HEALTH": "0",
            "WATCHDOG_DIGEST_STALE_ISSUES": "0",
            "WATCHDOG_DIGEST_INCLUDE_STABILITY": "0",
        },
        deliverer=deliver,
        delivery_fetcher=fetcher,
    )
    assert rc == 0
    return texts[-1], json.dumps(cards[-1], ensure_ascii=False)


def test_the_weekly_digest_text_and_card_carry_the_delivery_section(
    monkeypatch,
) -> None:
    seen = {}

    def fetcher(repo, token, *, since):
        seen.update(repo=repo, token=token, since=since)
        return _forty_records()

    text, card = _run_cli(monkeypatch, fetcher=fetcher)
    assert "Delivery (stage records):" in text
    assert "- staging: 6 runs, pass 66.7% (95% CI 30.0-90.3)" in text
    assert "交付记录 (Delivery)" in card and "staging: 6 runs, pass 66.7%" in card
    assert seen["token"] == "tok"
    assert (
        timedelta(days=6)
        < datetime.now(timezone.utc) - seen["since"]
        < timedelta(days=8)
    )


def test_a_failing_read_of_the_records_shows_one_line_and_the_digest_still_goes_out(
    monkeypatch,
) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("api down")

    text, card = _run_cli(monkeypatch, fetcher=broken)
    assert "stage records unavailable (RuntimeError)" in text
    assert "stage records unavailable (RuntimeError)" in card


def test_without_a_fetcher_the_digest_has_no_delivery_section(monkeypatch) -> None:
    text, _ = _run_cli(monkeypatch, fetcher=None)
    assert "Delivery" not in text


def test_the_digest_entry_point_reads_the_records_with_the_reader(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        watchdog_weekly_digest, "digest_main", lambda **kw: captured.update(kw) or 0
    )
    assert watchdog_weekly_digest.main(["--monthly"]) == 0
    assert captured == {
        "argv": ["--monthly"],
        "delivery_fetcher": stage_records.list_records,
    }
