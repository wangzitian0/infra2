"""Behavioural KV put() budget for the Cloudflare watchdog worker.

Runs worker.js under node against an in-memory KV and a fake clock for a whole UTC
day (libs/tests/fixtures/watchdog_kv_budget_harness.mjs). 2026-09-15/16 spent 1198
and 1157 of the account's 1000 free puts/day: the probe runner's liveness ping
(ok=true) alternated with a failing verdict (ok=false), the worker wrote every status
change at once, and a quota-exhausted day froze both heartbeats into false pages.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "cloudflare/infra-watchdog/worker.js"
WRANGLER = ROOT / "cloudflare/infra-watchdog/wrangler.toml"
HARNESS = Path(__file__).resolve().parent / "fixtures/watchdog_kv_budget_harness.mjs"
NODE = shutil.which("node")
KV_FREE_DAILY_PUTS = 1000
CRON_RUNS_PER_DAY = 48

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(scope="module")
def budget_vars() -> dict[str, str]:
    return tomllib.loads(WRANGLER.read_text(encoding="utf-8"))["vars"]


@pytest.fixture(scope="module")
def day(tmp_path_factory, budget_vars) -> dict:
    work = tmp_path_factory.mktemp("watchdog-kv")
    worker = work / "worker.mjs"  # worker.js is an ES module with no package.json
    worker.write_text(WORKER.read_text(encoding="utf-8"), encoding="utf-8")
    variables = work / "vars.json"
    variables.write_text(json.dumps(budget_vars), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(HARNESS), str(worker), str(variables)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _per_key_bound(budget_vars: dict[str, str]) -> int:
    interval = int(budget_vars["WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS"])
    status_changes = int(budget_vars["WATCHDOG_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY"])
    return math.ceil(86400 / interval) + status_changes


def test_a_failing_probe_costs_refresh_writes_not_one_write_per_post(
    day, budget_vars
) -> None:
    interval = int(budget_vars["WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS"])
    refreshes = math.ceil(86400 / interval)
    # The old worker wrote all ~2600 posts of this day (every post a status change).
    for scenario in ("failing", "failingLegacyRunner", "healthy"):
        assert day[scenario]["puts"] <= refreshes, scenario


def test_the_liveness_ping_never_hides_a_failing_verdict(day) -> None:
    for scenario in ("failing", "failingLegacyRunner"):
        assert day[scenario]["storedOk"], scenario
        assert not any(day[scenario]["storedOk"]), scenario
    assert all(day["healthy"]["storedOk"])


def test_flapping_verdicts_stay_inside_the_per_key_bound(day, budget_vars) -> None:
    for scenario in ("flapping", "flappingNoLiveness"):
        assert day[scenario]["puts"] <= _per_key_bound(budget_vars), scenario


def test_malformed_budget_vars_fall_back_to_the_defaults(day) -> None:
    """#735 review: a NaN or zero interval must not reopen write-every-post."""
    default_interval = 600  # DEFAULT_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS
    default_budget = 24  # DEFAULT_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY
    bound = math.ceil(86400 / default_interval) + default_budget
    for scenario in (
        "malformedInterval",
        "emptyInterval",
        "zeroInterval",
        "malformedBudget",
    ):
        assert day[scenario]["puts"] <= bound, (scenario, day[scenario]["puts"])


def test_heartbeats_never_look_stale_to_the_cron(day, budget_vars) -> None:
    max_age = min(
        heartbeat["maxAgeSeconds"]
        for heartbeat in json.loads(budget_vars["WATCHDOG_HEARTBEATS_JSON"])
    )
    for scenario in ("healthy", "failing", "flapping", "livenessOnly"):
        assert day[scenario]["maxAgeSeconds"] < max_age, scenario


def test_liveness_pings_alone_keep_the_record_fresh_and_the_verdict(
    day, budget_vars
) -> None:
    interval = int(budget_vars["WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS"])
    only = day["livenessOnly"]
    assert only["puts"] <= math.ceil(86400 / (2 * interval))  # one per two intervals
    assert not any(only["storedOk"])  # the seeded failing verdict survives


def test_a_recovery_is_written_at_once_and_a_ping_does_not_fake_one(day) -> None:
    assert day["recovery"] == {
        "okAfterPing": False,
        "okAfterVerdict": True,
        "reason": "status-change",
        "puts": 2,
    }


def test_an_unconfigured_heartbeat_name_costs_no_put(day) -> None:
    assert day["unconfigured"] == {"status": 404, "puts": 0}


def test_a_cron_day_costs_at_most_three_puts_per_run(day) -> None:
    assert day["cronDay"]["puts"] <= CRON_RUNS_PER_DAY * 3
    assert day["cronDay"]["putsByKey"]["watchdog:last-run"] == CRON_RUNS_PER_DAY


def test_worst_case_day_is_under_half_the_free_tier(day, budget_vars) -> None:
    keys = day["heartbeatKeys"]
    bound = keys * _per_key_bound(budget_vars) + CRON_RUNS_PER_DAY * 3
    measured = (
        keys * max(day[s]["puts"] for s in ("healthy", "failing", "flapping"))
        + day["cronDay"]["puts"]
    )
    assert measured <= bound < KV_FREE_DAILY_PUTS * 0.5, (measured, bound)
