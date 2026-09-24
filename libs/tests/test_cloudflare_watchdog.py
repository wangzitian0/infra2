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

from libs.alerting import FIELD_SEPARATOR, PAGER_FIELDS, pager_level, since_text

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


def _blocks(message: str) -> list[dict[str, str]]:
    """The event blocks of a Worker message, each ``{label: value}`` plus its
    ``section`` (firing / resolved) and ``heading``, in message order (#905)."""
    blocks: list[dict[str, str]] = []
    section = "resolved" if message.startswith("✅") else "firing"
    for line in message.splitlines():
        if line.startswith("✅ 已恢复 "):
            section = "resolved"
        elif line.startswith("— ") and line.endswith(" —"):
            blocks.append({"section": section, "heading": line})
        elif blocks:
            label, separator, value = line.partition(FIELD_SEPARATOR)
            if separator and label in PAGER_FIELDS:
                blocks[-1][label] = value
    return blocks


def _block(message: str, section: str, name: str) -> dict[str, str]:
    """The one ``section`` block whose object is ``name``."""
    (found,) = [
        block
        for block in _blocks(message)
        if block["section"] == section and block["对象"].endswith(f" · {name}")
    ]
    return found


def _names(message: str, section: str) -> list[str]:
    return [
        block["对象"].rsplit(" · ", 1)[-1]
        for block in _blocks(message)
        if block["section"] == section
    ]


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
    page = _block(first, "firing", "dokploy-public-route")
    assert (page["级别"], page["环境"], page["对象"]) == (
        "P0",
        "production",
        "bootstrap/dokploy · dokploy-public-route",
    )
    assert page["影响"].startswith("[host-reachability] ")
    again = _block(renotify, "firing", "dokploy-public-route")
    assert again["heading"] == "— 1/1 · 仍在告警 —"
    # the incident started at the first failing run, not at the page
    assert page["开始于"] == "2026-09-24 08:00（UTC+8）(已持续 30 分钟)"
    assert again["开始于"] == "2026-09-24 08:00（UTC+8）(已持续 6 小时 30 分钟)"
    assert all(run["error"] is None for run in runs)


@needs_node
def test_resolved_names_what_recovered(world) -> None:
    resolved = world["entrypointDown"][4]["messages"][0]
    assert resolved.startswith("✅ [已恢复] Cloudflare 带外 watchdog · 1 项")
    block = _block(resolved, "resolved", "dokploy-public-route")
    assert block["现象"] == "https://cloud.zitian.party 已恢复可达(HTTP 200)"
    # what recovered, and how long it was down: first failing run to recovery
    assert block["开始于"] == (
        "2026-09-24 08:00（UTC+8） → 2026-09-24 15:00（UTC+8）(共 7 小时)"
    )
    assert _names(resolved, "firing") == []


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
    # the loop P1: streak opens, fires, holds (no write), recovery opens, resolves
    assert world["loopUnhealthy"]["putKeys"] == [
        ["watchdog:state"],
        ["watchdog:state"],
        ["watchdog:last-run"],
        ["watchdog:state"],
        ["watchdog:state"],
    ]


@needs_node
def test_a_network_error_counts_as_a_failure(world) -> None:
    (message,) = world["networkError"]
    page = _block(message, "firing", "truealpha-web-public-route")
    assert page["现象"].endswith("— 请求失败:connection refused(连续 2 次运行失败)")


@needs_node
def test_a_route_the_vps_already_reports_failing_is_suppressed(world) -> None:
    suppressed = world["suppressed"]
    assert suppressed["messages"] == []
    assert suppressed["lastRunSuppressed"] == ["finance-report-web-public-route"]
    (entry,) = suppressed["entrypoints"]
    assert entry["key"] == "production:finance-report-web-public-route"
    assert entry["suppressedReason"] == (
        "production/platform-alerting-probes heartbeat: "
        "finance-report-web-public-route paged in-band and still failing"
    )
    # recorded once, not rewritten every run
    assert suppressed["stateWrites"] == 2


@needs_node
def test_a_suppressed_entrypoint_pages_as_soon_as_suppression_stops(world) -> None:
    """Re-evaluated every run: no second debounce once the VPS stops listing it."""
    (message,) = world["suppressed"]["released"]
    assert _names(message, "firing") == ["finance-report-web-public-route"]


@needs_node
@pytest.mark.parametrize(
    "case",
    [
        "loopUnhealthy",
        "deliveryPredatesFailure",
        "neverDelivered",
        "maintenance",
        "neverPaged",
    ],
)
def test_the_worker_does_not_take_the_vps_at_its_word(world, case) -> None:
    """#911 review (HIGH): a listed route is suppressed only if the loop is healthy
    and delivered something after the last run that saw the route healthy; an empty
    list (maintenance) or a list without the route (never paged) never suppresses."""
    messages = world["notSuppressed"][case]
    assert len(messages) == 1
    assert "finance-report-web-public-route" in _names(messages[0], "firing")


@needs_node
def test_a_suppressed_entrypoint_pages_once_the_vps_heartbeat_goes_stale(world) -> None:
    """The VPS paged the route, then died: its last record still lists the route
    and a recent-enough delivery, but a stale heartbeat speaks for nothing."""
    runs = world["suppressedThenVpsDies"]
    # last fresh record at 59 min; stale (> 90 min old) from the 150-minute run on
    assert [run["minutes"] for run in runs] == [0, 30, 60, 90, 120, 150]
    assert [run["entrypointFiring"] for run in runs] == [False] * 5 + [True]
    assert [run["vpsDown"] for run in runs] == [False] * 5 + [True]


@needs_node
def test_a_delivery_after_the_last_healthy_run_is_recent_enough(world) -> None:
    """The failure began after the previous (healthy) run, one cron interval before
    the Worker first saw it; a delivery since then may be the VPS's page for it."""
    assert world["deliveryAfterLastHealthyRun"] == []


@needs_node
def test_an_unknown_route_list_never_suppresses(world) -> None:
    """A v1 heartbeat carries no route list; a stale one is not evidence of anything."""
    (v1,) = world["v1Heartbeat"]
    assert _names(v1, "firing") == ["finance-report-web-public-route"]
    assert "finance-report-web-public-route" in [
        name
        for message in world["staleHeartbeat"]
        for name in _names(message, "firing")
    ]


# ---- heartbeats --------------------------------------------------------------------


@needs_node
def test_a_stale_production_heartbeat_pages_p0_vps_down_once(world) -> None:
    down = world["vpsDown"]
    firing, resolved = down["messages"]
    page = _block(firing, "firing", "platform-alerting-probes")
    assert page["级别"] == "P0"
    assert page["现象"].startswith("VPS 或它的出网中断 — 心跳过期:")
    assert page["影响"].startswith("[host-reachability] VPS")
    assert page["Runbook"].endswith("docs/runbooks/infra022-p0.md#watchdog-silent")
    recovered = _block(resolved, "resolved", "platform-alerting-probes")
    assert recovered["现象"].startswith("心跳恢复新鲜(")


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
def test_an_unhealthy_loop_pages_p1_after_two_runs_and_resolves_after_two(
    world,
) -> None:
    messages = world["loopUnhealthy"]["messages"]
    # unhealthy, unhealthy (fire), unhealthy, healthy, healthy (resolve)
    assert [len(run) for run in messages] == [0, 1, 0, 0, 1]
    (fired,) = messages[1]
    assert fired.startswith("🟠 [P1 告警] ")
    page = _block(fired, "firing", "platform-alerting-probes")
    assert page["级别"] == "P1"
    assert page["影响"].startswith("[alert-pipeline] ")
    assert page["现象"] == (
        "探测循环报告不健康 — alert bridge delivery failing for 42 min"
    )
    (resolved,) = messages[4]
    assert _names(resolved, "resolved") == ["platform-alerting-probes"]


@needs_node
def test_a_flapping_loop_never_pages(world) -> None:
    """#911 review: one flip per run paged and resolved 48 times a day."""
    assert world["loopFlapping"] == []


@needs_node
def test_a_v1_runners_ok_false_is_not_a_broken_loop(world) -> None:
    """A v1 runner sends ok=false whenever any probe fails: unknown, never a P1."""
    assert world["v1LoopFalse"] == []


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
    assert sorted(_names(fired, "firing")) == [
        "dokploy-public-route",
        "platform-alerting-probes",
    ]
    assert partial["firstHealthy"] == []  # one healthy run does not resolve the loop
    (resolved,) = partial["resolved"]
    assert _names(resolved, "resolved") == ["platform-alerting-probes"]
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
    assert _names(retried, "firing") == ["dokploy-public-route"]


@needs_node
def test_a_feishu_outage_escalates_to_email(world) -> None:
    escalation = world["emailEscalation"]
    assert escalation["error"] is None
    (email,) = escalation["messages"]
    assert email.startswith("EMAIL 🔴 [P0 告警] Cloudflare 带外 watchdog")
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
    page = _block(message, "firing", "cloudflare-watchdog-config-preflight")
    assert (page["级别"], page["环境"]) == ("P1", "global")
    assert page["影响"].startswith("[watchdog-config] ")
    assert page["现象"].startswith("Worker 无法评估它的检查 — 配置预检失败:")
    assert _names(message, "resolved") == []
    assert broken["lastRunOk"] is False
    assert "production:dokploy-public-route:entrypoint" in broken["stillActive"]


@needs_node
def test_status_stays_far_inside_githubs_4096_byte_read(world) -> None:
    """#911 review: every served text is cut, even from records stored untrimmed."""
    size = world["statusSize"]
    assert size["status"] == 200
    assert size["bytes"] < 3072
    # a runner cannot store more than 300 characters of detail
    assert size["storedDetailLength"] == 300
    assert size["storedRoutes"] == 32
    # a route name longer than any probe name makes the list unknown
    assert size["overlongRouteList"] is None


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


# ---- the pager layout (#905) ---------------------------------------------------------

#: 2026-09-24 00:00 UTC, the harness clock's start.
HARNESS_START = 1_790_208_000


@needs_node
def test_worker_messages_follow_the_shared_field_order(world) -> None:
    """#905: the Worker's text uses libs/alerting.py's labels, separator and order: a
    page shows every field but the log tail, a recovery 级别 through 开始于."""
    runs = world["entrypointDown"]
    (page,) = _blocks(runs[1]["messages"][0])
    (recovery,) = _blocks(runs[4]["messages"][0])

    assert [key for key in page if key in PAGER_FIELDS] == list(PAGER_FIELDS[:8])
    assert [key for key in recovery if key in PAGER_FIELDS] == list(PAGER_FIELDS[:5])
    assert page["Runbook"].endswith(
        "platform/12.alerting/README.md#public-route-probes"
    )
    assert page["下一步"] == (
        '从外部网络执行 `curl -I "https://cloud.zitian.party"`;'
        "若 VPS 心跳也过期,就是整机或它的出网中断"
    )


@needs_node
def test_worker_times_read_like_the_bridges(world) -> None:
    """#905: 开始于 is written the way libs.alerting.since_text writes it."""
    runs = world["entrypointDown"]
    page = _block(runs[1]["messages"][0], "firing", "dokploy-public-route")
    recovery = _block(runs[4]["messages"][0], "resolved", "dokploy-public-route")

    assert page["开始于"] == since_text(HARNESS_START, now=HARNESS_START + 1800)
    assert recovery["开始于"] == since_text(
        HARNESS_START, now=0, end=HARNESS_START + 7 * 3600
    )


@needs_node
@pytest.mark.parametrize(
    ("severity", "level"),
    [
        ("critical", "P0"),
        ("error", "P1"),
        ("warning", "P2"),
        ("P1", "P1"),
        ("p2", "P2"),
        ("garbage", "P0"),
    ],
)
def test_worker_levels_follow_the_ssot(world, severity, level) -> None:
    """#905: one vocabulary. A target's severity renders as §3 maps it -- warning is
    P2, not P1 -- in the block and in the title, exactly as the bridge maps it."""
    (message,) = world["severityLevels"][severity]
    page = _block(message, "firing", "dokploy-public-route")

    assert page["级别"] == level == pager_level(severity)
    assert message.splitlines()[0] == f"{_EMOJI[level]} [{level} 告警] " + (
        "Cloudflare 带外 watchdog · 1 项"
    )


_EMOJI = {"P0": "🔴", "P1": "🟠", "P2": "🟡"}


@needs_node
def test_a_worker_message_with_many_failures_stays_bounded(world) -> None:
    """#905: twelve entrypoints down in one run. The most severe come first, as many
    as fit are shown in full, every other one is still named on a summary line, and
    the text stays inside Feishu's limit."""
    _stale, message = world["manyDown"]
    full = _blocks(message)
    summary = [line for line in message.splitlines() if line.startswith("• ")]
    names = [f"product-{index:02d}-public-route" for index in range(12)]

    assert len(message) <= 3500
    assert message.splitlines()[0] == "🟠 [P1 告警] Cloudflare 带外 watchdog · 12 项"
    assert 1 <= len(full) < 12
    assert len(full) + len(summary) == 12
    assert f"另有 {len(summary)} 项,只列摘要:" in message
    # every P1 (even index) before every P2 (odd index)
    shown = [block["对象"].rsplit(" · ", 1)[-1] for block in full] + [
        line.split(" · ")[3] for line in summary
    ]
    assert sorted(shown) == names
    assert [name[8:10] for name in shown] == [
        *(f"{index:02d}" for index in range(0, 12, 2)),
        *(f"{index:02d}" for index in range(1, 12, 2)),
    ]


@needs_node
def test_every_runbook_the_worker_links_resolves(world) -> None:
    """#905: each failure class links a specific anchor, and the anchor exists."""
    from libs.tests.test_pager_format import BLOB, _anchors

    messages = [
        *world["entrypointDown"][1]["messages"],
        *world["vpsDown"]["messages"],
        *world["loopUnhealthy"]["messages"][1],
        *world["configBroken"]["messages"],
    ]
    links = {
        (block["影响"].split("]")[0][1:], block["对象"].rsplit(" · ", 1)[-1]): block[
            "Runbook"
        ]
        for message in messages
        for block in _blocks(message)
        if block["section"] == "firing"
    }

    assert links == {
        ("host-reachability", "dokploy-public-route"): (
            f"{BLOB}/platform/12.alerting/README.md#public-route-probes"
        ),
        ("host-reachability", "platform-alerting-probes"): (
            f"{BLOB}/docs/runbooks/infra022-p0.md#watchdog-silent"
        ),
        ("alert-pipeline", "platform-alerting-probes"): (
            f"{BLOB}/platform/12.alerting/README.md#infra-service-probes"
        ),
        ("watchdog-config", "cloudflare-watchdog-config-preflight"): (
            f"{BLOB}/cloudflare/infra-watchdog/README.md#optional-vars"
        ),
    }
    urls = set(links.values())
    missing = [
        url
        for url in sorted(urls)
        if url.split("#", 1)[1]
        not in _anchors(ROOT / url.removeprefix(f"{BLOB}/").split("#", 1)[0])
    ]
    assert missing == []


@needs_node
def test_worker_prose_is_chinese(world) -> None:
    """#905: the Worker's next steps, impacts and failure summaries are Chinese;
    commands and identifiers stay verbatim in backticks."""
    from libs.tests.test_pager_format import english_prose

    messages = [
        *world["entrypointDown"][1]["messages"],
        *world["vpsDown"]["messages"],
        *world["loopUnhealthy"]["messages"][1],
        *world["configBroken"]["messages"],
    ]
    firing = [
        block
        for message in messages
        for block in _blocks(message)
        if block["section"] == "firing"
    ]
    texts = [
        text
        for block in firing
        for text in (block["下一步"], block["影响"], block["现象"].split(" — ")[0])
    ]

    assert len(firing) == 4
    assert {text: english_prose(text) for text in texts if english_prose(text)} == {}
