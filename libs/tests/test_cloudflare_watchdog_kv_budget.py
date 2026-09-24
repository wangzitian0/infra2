"""Behavioural KV put() and subrequest budget for the Cloudflare watchdog worker.

Runs worker.js under node against an in-memory KV and a fake clock for a whole UTC
day (libs/tests/fixtures/watchdog_kv_budget_harness.mjs). 2026-09-15/16 spent 1198
and 1157 of the account's 1000 free puts/day: the probe runner's liveness ping
(ok=true) alternated with a failing verdict (ok=false), the worker wrote every status
change at once, and a quota-exhausted day froze both heartbeats into false pages.

#904 budget (ops.observability.md §1.1): at most 10 subrequests per cron run (fetch +
KV, the Feishu token and send included) and at most 200 KV writes a day. CPU time
cannot be measured here; it is measured after the approved deploy (target: cron CPU
p99 < 5 ms).
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
KV_DAILY_PUT_BUDGET = 200  # #904
SUBREQUESTS_PER_RUN_BUDGET = 10  # #904
CRON_RUNS_PER_DAY = 48
CRON_PUTS_PER_RUN = 1

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


# The clamps worker.js applies whatever the env says. Proven by behaviour below: a
# tiny, zero or malformed interval and a huge budget still write no more than these
# allow (the scenarios in test_out_of_range_budget_vars_cannot_raise_the_runtime_bound).
RUNTIME_MIN_INTERVAL = 600
RUNTIME_MAX_STATUS_CHANGES = 24


def test_out_of_range_budget_vars_cannot_raise_the_runtime_bound(
    day, budget_vars
) -> None:
    """#735 review: a NaN, zero or tiny interval, or a huge budget, must not reopen
    write-every-post, whatever the Worker env says."""
    per_key = math.ceil(86400 / RUNTIME_MIN_INTERVAL) + RUNTIME_MAX_STATUS_CHANGES
    for scenario in (
        "malformedInterval",
        "emptyInterval",
        "zeroInterval",
        "tinyInterval",
        "malformedBudget",
        "hugeBudget",
    ):
        assert day[scenario]["puts"] <= per_key, (scenario, day[scenario]["puts"])
    heartbeat_keys = len(json.loads(budget_vars["WATCHDOG_HEARTBEATS_JSON"]))
    runtime_worst_case = (
        heartbeat_keys * per_key + CRON_RUNS_PER_DAY * CRON_PUTS_PER_RUN
    )
    assert runtime_worst_case < KV_FREE_DAILY_PUTS * 0.5


def test_a_corrupted_budget_counter_does_not_disable_the_budget(
    day, budget_vars
) -> None:
    """#735 review: a NaN or negative stored counter must not reopen early writes."""
    interval = int(budget_vars["WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS"])
    refreshes = math.ceil(86400 / interval)
    # a non-numeric or negative counter counts as spent: refresh writes only
    # (the previous head wrote 116 and 1045 puts for these days)
    for scenario in ("corruptBudgetCounter", "negativeBudgetCounter"):
        assert day[scenario]["puts"] <= refreshes + 1, scenario


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


def test_a_cron_run_writes_exactly_one_key_even_when_every_run_transitions(
    day,
) -> None:
    """The state document on a transition, else the last-run record: never both."""
    cron = day["cronDay"]
    assert cron["putsPerRun"] == [CRON_PUTS_PER_RUN] * CRON_RUNS_PER_DAY
    assert set(cron["putsByKey"]) == {"watchdog:state", "watchdog:last-run"}
    # every run of this day is a transition, so all but the first write the state
    assert cron["putsByKey"]["watchdog:state"] >= CRON_RUNS_PER_DAY - 1


def test_worst_case_day_is_within_the_904_budget(day, budget_vars) -> None:
    keys = day["heartbeatKeys"]
    bound = keys * _per_key_bound(budget_vars) + CRON_RUNS_PER_DAY * CRON_PUTS_PER_RUN
    measured = (
        keys * max(day[s]["puts"] for s in ("healthy", "failing", "flapping"))
        + day["cronDay"]["puts"]
    )
    assert measured <= bound <= KV_DAILY_PUT_BUDGET, (measured, bound)


# ---- #904: whole simulated days, both runners and the cron together -------------


def test_a_healthy_day_stays_within_both_budgets(day) -> None:
    healthy = day["healthyDay"]
    assert healthy["cronRuns"] == CRON_RUNS_PER_DAY
    assert healthy["puts"] <= KV_DAILY_PUT_BUDGET
    assert healthy["maxSubrequests"] <= SUBREQUESTS_PER_RUN_BUDGET
    assert healthy["messages"] == 0
    # a healthy day never touches the state document
    assert "watchdog:state" not in healthy["putsByKey"]


def test_a_day_with_every_entrypoint_down_stays_within_both_budgets(day) -> None:
    down = day["entrypointsDownDay"]
    assert down["puts"] <= KV_DAILY_PUT_BUDGET
    assert down["maxSubrequests"] <= SUBREQUESTS_PER_RUN_BUDGET
    # paged once, then renotified every 6 h: 00:30, 06:30, 12:30, 18:30
    assert down["messages"] == 4
    # the alert runs are the dearest: token + send on top of a quiet run
    assert max(down["subrequestsPerRun"]) == min(down["subrequestsPerRun"]) + 2


def test_the_worst_flapping_day_stays_within_both_budgets(day, budget_vars) -> None:
    worst = day["worstDay"]
    assert worst["puts"] <= KV_DAILY_PUT_BUDGET
    assert worst["maxSubrequests"] <= SUBREQUESTS_PER_RUN_BUDGET
    # it did exercise the expensive paths: status-change writes and a state write a run
    for key in (
        "heartbeat:production:platform-alerting-probes",
        "heartbeat:staging:platform-alerting-probes-staging",
    ):
        assert worst["putsByKey"][key] > 86400 // int(
            budget_vars["WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS"]
        )
    assert worst["putsByKey"]["watchdog:state"] >= CRON_RUNS_PER_DAY - 1
