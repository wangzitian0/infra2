"""Behavioural contract of the Cloudflare out-of-band watchdog (#904).

The Worker runs under node against an in-memory KV, a fake clock and a fake network
(libs/tests/fixtures/watchdog_worker_harness.mjs). Nothing here reads worker.js as
text: the old source-grep tests passed while the deployed config had drifted from
the code (#901 M2). Scope (ops.observability.md §1.1): the production heartbeat's
freshness and loop health, and one external entrypoint per product.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "cloudflare/infra-watchdog"
WORKER = WORKER_DIR / "worker.js"
WRANGLER = WORKER_DIR / "wrangler.toml"
README = WORKER_DIR / "README.md"
HARNESS = Path(__file__).resolve().parent / "fixtures/watchdog_worker_harness.mjs"
SIGNALS = ROOT / "docs/ssot/watchdog-signals.yaml"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _vars() -> dict[str, str]:
    return tomllib.loads(WRANGLER.read_text(encoding="utf-8"))["vars"]


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> dict:
    work = tmp_path_factory.mktemp("watchdog-worker")
    module = work / "worker.mjs"  # worker.js is an ES module with no package.json
    module.write_text(WORKER.read_text(encoding="utf-8"), encoding="utf-8")
    variables = work / "vars.json"
    variables.write_text(json.dumps(_vars()), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(HARNESS), str(module), str(variables)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _entrypoint_urls() -> list[str]:
    return [target["url"] for target in json.loads(_vars()["WATCHDOG_TARGETS_JSON"])]


# ---- what a run costs ----------------------------------------------------------


@needs_node
def test_a_quiet_run_is_three_gets_two_reads_one_write_and_the_deadman(world) -> None:
    quiet = world["quiet"]
    assert quiet["error"] is None
    assert quiet["fetched"] == [
        *_entrypoint_urls(),
        "https://hc-ping.com/test-worker-check",
    ]
    assert (quiet["kvGets"], quiet["kvPuts"], quiet["kvDeletes"]) == (2, 1, 0)
    assert quiet["putKeys"] == ["watchdog:last-run"]
    assert quiet["subrequests"] == 7
    assert quiet["messages"] == []


@needs_node
def test_no_run_sleeps_or_retries_inside_the_invocation(world) -> None:
    """#901: the 60 s in-run retry is gone; a failing entrypoint is fetched once."""
    assert world["quiet"]["firedTimers"] == []
    assert [run["firedTimers"] for run in world["allDown"]] == [[], [], []]
    # each entrypoint once, plus the dead-man ping, plus token and send on alert runs
    assert [run["fetched"] for run in world["allDown"]] == [6, 6, 4]


@needs_node
def test_every_run_with_all_entrypoints_down_stays_within_ten_subrequests(
    world,
) -> None:
    runs = world["allDown"]
    assert [run["kvPuts"] for run in runs] == [1, 1, 1]
    assert max(run["subrequests"] for run in runs) <= 10
    # the alert runs are the expensive ones, and they did deliver
    assert [len(run["messages"]) for run in runs] == [1, 1, 0]


# ---- config: the built-in defaults are the deployed config -----------------------


@needs_node
def test_default_targets_and_heartbeats_equal_the_deployed_config(world) -> None:
    """#901 M2: the full-dependency route lived only in DEFAULT_TARGETS and was never
    probed. The defaults and wrangler.toml must describe the same checks."""
    defaults = world["defaults"]
    assert defaults["defaultFetched"] == defaults["configuredFetched"]
    assert defaults["defaultHeartbeats"] == defaults["configuredHeartbeats"]


def test_the_worker_targets_are_exactly_the_registry_entrypoints() -> None:
    """One external entrypoint per product, production only; the rest is in-band."""
    import yaml

    signals = yaml.safe_load(SIGNALS.read_text(encoding="utf-8"))["signals"]
    entrypoints = {
        (s["environment"], s["signal"], s["url"])
        for s in signals
        if s["primary_owner"] == "cloudflare"
        and s.get("failure_class") == "host-reachability"
    }
    targets = {
        (t["environment"], t["name"], t["url"])
        for t in json.loads(_vars()["WATCHDOG_TARGETS_JSON"])
    }
    assert targets == entrypoints
    assert {environment for environment, _, _ in targets} == {"production"}
    assert len(targets) == 3


def test_wrangler_config_drops_retries_and_the_archive() -> None:
    config = tomllib.loads(WRANGLER.read_text(encoding="utf-8"))
    variables = config["vars"]
    assert config["triggers"]["crons"] == ["*/30 * * * *"]
    assert [kv["binding"] for kv in config["kv_namespaces"]] == ["WATCHDOG_STATE"]
    assert "r2_buckets" not in config
    assert not {"WATCHDOG_RETRY_MAX_ATTEMPTS", "WATCHDOG_RETRY_DELAY_MS"} & set(
        variables
    )
    assert variables["WATCHDOG_ENTRYPOINT_FAILURE_RUNS"] == "2"
    assert variables["WATCHDOG_RENOTIFY_SECONDS"] == str(6 * 3600)
    assert config["observability"]["enabled"] is True
    # secrets never live in the committed config
    raw = WRANGLER.read_text(encoding="utf-8")
    assert "FEISHU_WEBHOOK_URL" not in raw and "HEARTBEAT_TOKEN" not in raw


# ---- entrypoints -----------------------------------------------------------------


@needs_node
def test_an_entrypoint_pages_after_two_failing_runs_then_every_six_hours(world) -> None:
    runs = world["entrypointDown"]
    counts = [len(run["messages"]) for run in runs]
    # run 1 fails (pending), run 2 pages, run 3 is quiet, +6 h renotifies, recovery
    assert counts == [0, 1, 0, 1, 1]
    first, renotify = runs[1]["messages"][0], runs[3]["messages"][0]
    assert first.splitlines()[2].startswith(
        "FIRING P0 host-reachability production/dokploy-public-route"
    )
    assert renotify.splitlines()[2].startswith(
        "STILL FIRING P0 host-reachability production/dokploy-public-route"
    )
    # the incident started at the first failing run, not at the page
    assert "Since: 2026-09-24T00:00:00.000Z" in first
    assert all(run["error"] is None for run in runs)


@needs_node
def test_resolved_names_what_recovered(world) -> None:
    resolved = world["entrypointDown"][4]["messages"][0]
    assert resolved.startswith("[RESOLVED]")
    assert "RESOLVED production/dokploy-public-route (host-reachability)" in resolved
    assert "https://cloud.zitian.party reachable again (HTTP 200)" in resolved


@needs_node
def test_kv_is_written_on_transitions_only(world) -> None:
    runs = world["entrypointDown"]
    # first failure, page, quiet, renotify, recovery
    assert [run["putKeys"] for run in runs] == [
        ["watchdog:state"],
        ["watchdog:state"],
        ["watchdog:last-run"],
        ["watchdog:state"],
        ["watchdog:state"],
    ]
    assert world["loopUnhealthy"]["repeatPutKeys"] == ["watchdog:last-run"]


@needs_node
def test_a_network_error_counts_as_a_failure(world) -> None:
    (message,) = world["networkError"]
    assert "production/truealpha-web-public-route" in message
    assert "fetch failed: connection refused" in message


@needs_node
def test_a_route_the_vps_already_reports_failing_is_suppressed(world) -> None:
    suppressed = world["suppressed"]
    assert suppressed["messages"] == []
    assert suppressed["lastRunSuppressed"] == ["finance-report-web-public-route"]
    (entry,) = suppressed["entrypoints"]
    assert entry["key"] == "production:finance-report-web-public-route"
    assert "platform-alerting-probes heartbeat lists" in entry["suppressedReason"]
    # recorded once, not rewritten every run
    assert suppressed["stateWrites"] == 2


@needs_node
def test_an_unknown_route_list_never_suppresses(world) -> None:
    """A v1 heartbeat carries no route list; a stale one is not evidence of anything."""
    (v1,) = world["v1Heartbeat"]
    assert (
        "FIRING P0 host-reachability production/finance-report-web-public-route" in v1
    )
    stale = "\n".join(world["staleHeartbeat"])
    assert (
        "FIRING P0 host-reachability production/finance-report-web-public-route"
        in stale
    )


# ---- heartbeats --------------------------------------------------------------------


@needs_node
def test_a_stale_production_heartbeat_pages_p0_vps_down_once(world) -> None:
    down = world["vpsDown"]
    firing, resolved = down["messages"]
    assert (
        "FIRING P0 host-reachability production/platform-alerting-probes: "
        "VPS or its egress is down" in firing
    )
    assert "heartbeat stale" in firing
    assert (
        "RESOLVED production/platform-alerting-probes (host-reachability)" in resolved
    )
    assert "heartbeat fresh again" in resolved


@needs_node
def test_an_outage_costs_one_write_down_and_one_up_and_is_served(world) -> None:
    down = world["vpsDown"]
    assert down["runs"] == 8
    assert down["statePuts"] == 2
    (outage,) = down["outages"]
    assert outage["environment"] == "production"
    assert outage["name"] == "platform-alerting-probes"
    # from the last recorded contact to the first fresh heartbeat after it
    assert outage["start"] == down["lastContact"]
    assert outage["end"] == down["recoveredAt"]


@needs_node
def test_an_unhealthy_loop_pages_p1_alert_pipeline_with_its_detail(world) -> None:
    unhealthy = world["loopUnhealthy"]
    (message,) = unhealthy["first"]
    assert message.startswith("[OUT-OF-BAND] P1 ")
    assert "FIRING P1 alert-pipeline production/platform-alerting-probes" in message
    assert "alert bridge delivery failing for 12 min" in message
    # a changing detail is the same failure identity: no second page
    assert unhealthy["repeat"] == []


@needs_node
def test_staging_is_recorded_for_status_and_never_paged(world) -> None:
    staging = world["staging"]
    assert staging["messages"] == []
    assert not any("staging" in url for url in staging["fetched"])
    by_env = {hb["environment"]: hb for hb in staging["heartbeats"]}
    assert by_env["staging"]["paged"] is False
    assert by_env["staging"]["state"] == "stale"
    assert by_env["staging"]["loopOk"] is False
    assert by_env["production"]["paged"] is True
    assert by_env["production"]["state"] == "fresh"


@needs_node
def test_each_failure_identity_resolves_on_its_own(world) -> None:
    partial = world["partialRecovery"]
    (fired,) = partial["fired"]
    assert "production/dokploy-public-route" in fired
    (resolved,) = partial["resolved"]
    assert "RESOLVED production/platform-alerting-probes (alert-pipeline)" in resolved
    assert "dokploy-public-route" not in resolved
    assert partial["stillActive"] == ["production:dokploy-public-route:entrypoint"]


# ---- delivery ----------------------------------------------------------------------


@needs_node
def test_an_undelivered_page_is_retried_and_fails_the_run(world) -> None:
    failed = world["deliveryFailure"]
    assert failed["error"].startswith(
        "watchdog delivery failed: Feishu tenant token failed"
    )
    assert failed["deadman"] == ["https://hc-ping.com/test-worker-check/fail"]
    assert failed["messagesOnFailure"] == []
    assert failed["statusOk"] is False and failed["lastRunOk"] is False
    assert "Feishu tenant token failed" in failed["deliveryError"]
    (retried,) = failed["retried"]
    assert "FIRING P0 host-reachability production/dokploy-public-route" in retried


@needs_node
def test_a_feishu_outage_escalates_to_email(world) -> None:
    escalation = world["emailEscalation"]
    assert escalation["error"] is None
    (email,) = escalation["messages"]
    assert email.startswith("EMAIL [OUT-OF-BAND] P0")
    assert "primary Feishu delivery failed" in email
    # token refused, email sent: 7 + token + email
    assert escalation["subrequests"] == 9
    # the dearest run of all: token ok, send refused, email sent -- still within 10
    assert escalation["sendFailedError"] is None
    assert escalation["sendFailedMessages"] == 1
    assert escalation["sendFailedSubrequests"] == 10


# ---- read endpoints ----------------------------------------------------------------


@needs_node
def test_status_reports_the_workers_own_health_not_the_targets(world) -> None:
    """GitHub pages watchdog-liveness from /status: a failing entrypoint the Worker
    paged is not a Worker failure, a stale run record is."""
    status = world["status"]
    assert status["ok"] is True
    assert status["alertActive"] is True
    last_run = status["lastRun"]
    assert last_run["ok"] is True
    assert last_run["routeTargetCount"] == 3
    assert last_run["heartbeatTargetCount"] == 2
    assert last_run["failureCount"] == 1
    assert last_run["activeAlertCount"] == 1
    assert status["staleOk"] is False
    assert status["noTokenStatus"] == 401
    assert status["outagesNoTokenStatus"] == 401


@needs_node
def test_a_broken_config_pages_itself_and_resolves_nothing_it_could_not_check(
    world,
) -> None:
    broken = world["configBroken"]
    (message,) = broken["messages"]
    assert (
        "FIRING P1 watchdog-config global/cloudflare-watchdog-config-preflight"
        in message
    )
    assert "config-preflight failed" in message
    assert "RESOLVED" not in message
    assert broken["lastRunOk"] is False
    assert "production:dokploy-public-route:entrypoint" in broken["stillActive"]


@needs_node
def test_the_ledger_endpoint_is_gone(world) -> None:
    assert world["removedEndpoints"] == {"ledger": 404, "health": {"ok": True}}


# ---- heartbeat contract v2 -----------------------------------------------------------


@needs_node
def test_v2_heartbeat_fields_are_stored_sorted_and_deduplicated(world) -> None:
    hb = world["heartbeatV2"]
    assert hb["first"]["persisted"] is True
    stored = hb["storedFirst"]
    assert stored["schema"] == 2
    assert stored["lastDeliveryOkAt"] == 1790000000
    assert stored["failingPublicRoutes"] == [
        "dokploy-public-route",
        "vault-public-route",
    ]


@needs_node
def test_a_changed_failing_route_set_is_a_verdict_change(world) -> None:
    hb = world["heartbeatV2"]
    assert (hb["same"]["persisted"], hb["same"]["reason"]) == (False, "throttled")
    assert hb["changed"]["reason"] == "status-change"
    assert hb["storedChanged"]["failingPublicRoutes"] == ["vault-public-route"]
    assert hb["storedChanged"]["statusChangeBudget"]["used"] == 1


@needs_node
def test_a_liveness_ping_never_rewrites_the_route_set(world) -> None:
    hb = world["heartbeatV2"]
    assert hb["ping"]["reason"] == "liveness-refresh"
    assert hb["storedAfterPing"]["failingPublicRoutes"] == ["vault-public-route"]
    assert hb["storedAfterPing"]["refreshedBy"] == "liveness"


@needs_node
def test_v1_and_malformed_route_lists_are_unknown(world) -> None:
    hb = world["heartbeatV2"]
    legacy = hb["legacyStored"]
    assert (
        legacy["schema"],
        legacy["failingPublicRoutes"],
        legacy["lastDeliveryOkAt"],
    ) == (
        1,
        None,
        None,
    )
    assert legacy["ok"] is False
    assert hb["malformedStored"]["failingPublicRoutes"] is None


@needs_node
def test_a_kv_failure_on_heartbeat_degrades_instead_of_a_500(world) -> None:
    assert world["heartbeatV2"]["degraded"] == {
        "status": 200,
        "body": {
            "ok": True,
            "key": "heartbeat:production:platform-alerting-probes",
            "persisted": False,
            "degraded": True,
        },
    }


# ---- docs ---------------------------------------------------------------------------


def test_worker_docs_carry_the_deploy_and_secret_contract() -> None:
    readme = README.read_text(encoding="utf-8")
    for command in (
        "wrangler secret put FEISHU_WEBHOOK_URL",
        "wrangler secret put FEISHU_APP_SECRET",
        "wrangler secret put HEARTBEAT_TOKEN",
        "wrangler secret put WATCHDOG_STATUS_TOKEN",
        "wrangler kv namespace create WATCHDOG_STATE",
    ):
        assert command in readme
    assert "INFRA_PROBE_HEARTBEAT_URL" in readme
    assert "/outages" in readme
