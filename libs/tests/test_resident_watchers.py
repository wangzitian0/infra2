"""Tests for the single-resident-sidecar watcher-plugin surface (#543).

The behavior parity of the two migrated watchers themselves is proven by
test_container_breakdown.py (breakdown sweeps, #475 hysteresis incl. the
relapse property) and test_deploy_queue_guard.py (queue sweeps, renotify,
remediate/escalate) — both now exercising the libs/ watcher modules. This file
covers the plugin CONTRACT: registration, per-watcher pacing, failure
isolation, prod-only gating, the runner-loop integration, and the compose
topology (one sidecar, no separate watcher services).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from libs.container_breakdown_watch import BreakdownWatch
from libs.deploy_queue_guard import DeployQueueGuard
from libs.resident_watchers import ResidentWatcher, build_watchers

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform/12.alerting/compose.yaml"


def _load_probe_runner():
    path = ROOT / "tools/infra_probe_runner.py"
    spec = importlib.util.spec_from_file_location("infra_probe_runner_watchers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# plugin surface


def test_build_watchers_registers_breakdown_and_queue_guard() -> None:
    """ONE sidecar, ALL resident watching: both watchers come from the single
    registry, named after their registered watchdog signals."""
    watchers = build_watchers({"ENV": "production"})

    assert [w.name for w in watchers] == [
        "container-breakdown-watch",
        "deploy-queue-guard",
    ]
    assert all(isinstance(w, ResidentWatcher) for w in watchers)


def test_watcher_names_match_registered_signals() -> None:
    """no-new-wheels closure: each plugin's name IS a registered signal in
    watchdog-signals.yaml (the deploy-queue-guard entry is the #542 exemption
    redeemed by the #543 merge)."""
    signals = {
        s["signal"]
        for s in yaml.safe_load(
            (ROOT / "docs/ssot/watchdog-signals.yaml").read_text(encoding="utf-8")
        )["signals"]
    }

    for watcher in build_watchers({"ENV": "production"}):
        assert watcher.name in signals, (
            f"watcher {watcher.name!r} has no registered signal — register it "
            "in docs/ssot/watchdog-signals.yaml (#542 no-new-wheels)"
        )


def test_maybe_run_self_paces_on_the_watcher_interval() -> None:
    """A watcher whose interval exceeds the loop cadence skips iterations until
    due — the continuity mapping for the historical *_INTERVAL_SECONDS knobs."""

    class Probe(ResidentWatcher):
        name = "probe"
        interval_seconds = 120

        def __init__(self):
            super().__init__()
            self.sweeps = 0

        def _sweep(self):
            self.sweeps += 1

    w = Probe()
    assert w.maybe_run(now=1000.0) is True  # first tick always sweeps
    assert w.maybe_run(now=1060.0) is False  # 60s loop tick: not due yet
    assert w.maybe_run(now=1120.0) is True  # due again after its own interval
    assert w.sweeps == 2


def test_maybe_run_never_raises_and_retries_next_due_tick() -> None:
    """Failure isolation: one broken watcher logs and retries; it can never
    take down the probe loop or its sibling watchers."""

    class Broken(ResidentWatcher):
        name = "broken"
        interval_seconds = 60

        def __init__(self):
            super().__init__()
            self.attempts = 0

        def _sweep(self):
            self.attempts += 1
            raise RuntimeError("docker socket vanished")

    w = Broken()
    assert w.maybe_run(now=0.0) is True  # did not propagate
    assert w.maybe_run(now=60.0) is True  # retried on the next due tick
    assert w.attempts == 2


def test_breakdown_watch_is_prod_only_and_idles_elsewhere(monkeypatch) -> None:
    """The breakdown watcher reads the WHOLE shared Docker engine, so only the
    production runner's plugin sweeps — the staging plugin stays registered but
    idle (the standalone sidecar's exact gating)."""
    import libs.container_breakdown_watch as w

    def boom(sock):  # a non-prod plugin must never touch the socket
        raise AssertionError("staging plugin must not open the docker socket")

    monkeypatch.setattr(w, "_docker_client", boom)
    staging = BreakdownWatch({"ENV": "staging"})
    assert staging.enabled is False
    assert staging.maybe_run(now=0.0) is True  # ticked, swept as a no-op

    swept = {"n": 0}
    monkeypatch.setattr(w, "_docker_client", lambda sock: object())
    monkeypatch.setattr(w, "run_once", lambda *a, **k: swept.__setitem__("n", 1))
    prod = BreakdownWatch({"ENV": "production"})
    assert prod.enabled is True
    prod.maybe_run(now=0.0)
    assert swept["n"] == 1


def test_breakdown_env_triad_maps_into_config_for_continuity() -> None:
    """#543: the historical BREAKDOWN_* env names keep working, mapped into
    per-watcher config (the compose env block passes them through verbatim)."""
    w = BreakdownWatch(
        {
            "ENV": "production",
            "BREAKDOWN_INTERVAL_SECONDS": "300",
            "BREAKDOWN_RENOTIFY_SECONDS": "900",
            "BREAKDOWN_FAILURE_THRESHOLD": "4",
            "BREAKDOWN_RECOVERY_THRESHOLD": "6",
            "BREAKDOWN_LOG_TAIL": "50",
        }
    )

    assert w.interval_seconds == 300
    assert w.renotify == 900
    assert w.failure_threshold == 4
    assert w.recovery_threshold == 6
    assert w.log_tail == 50

    import libs.container_breakdown_watch as mod

    defaults = BreakdownWatch({"ENV": "production"})
    assert defaults.interval_seconds == mod.DEFAULT_INTERVAL
    assert defaults.failure_threshold == mod.DEFAULT_FAILURE_THRESHOLD
    assert defaults.recovery_threshold == mod.DEFAULT_RECOVERY_THRESHOLD


def test_deploy_queue_guard_runs_in_every_env() -> None:
    """The guard watches the shared Dokploy control plane; unlike the breakdown
    watcher it stays active on every env's runner (pre-merge behavior: the
    deploy-queue-guard service ran in prod AND staging)."""
    w = DeployQueueGuard({"ENV": "staging", "ALERTING_ENV_FILE": "/nonexistent"})
    assert w.name == "deploy-queue-guard"
    # no `enabled` gate exists on the guard — construction implies active
    assert not hasattr(w, "enabled")


# ---------------------------------------------------------------------------
# runner-loop integration


def test_probe_runner_loop_iterates_watchers_each_iteration(monkeypatch) -> None:
    """The merged loop invokes every registered watcher AFTER the probe cycle,
    every iteration, and keeps looping when a probe iteration fails."""
    runner = _load_probe_runner()
    events: list[str] = []

    class FakeWatcher:
        def __init__(self, name):
            self.name = name

        def maybe_run(self, now=None):
            events.append(self.name)
            return True

    def fake_run_once(**_kwargs):
        events.append("probes")
        if events.count("probes") == 1:
            raise RuntimeError("bridge unavailable")
        return 0

    def fake_sleep(_seconds):
        if events.count("probes") >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(
        runner, "_build_watchers", lambda: [FakeWatcher("a"), FakeWatcher("b")]
    )
    monkeypatch.setattr(runner, "run_once", fake_run_once)
    monkeypatch.setattr(runner, "_post_heartbeat", lambda **_k: None)
    monkeypatch.setattr(runner, "_touch_state", lambda _p: None)
    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--loop", "--json"])

    try:
        runner.main()
    except SystemExit:
        pass

    # watchers ran after the probes on BOTH iterations — including the one
    # whose probe cycle raised (watcher liveness must not depend on probe luck)
    assert events == ["probes", "a", "b", "probes", "a", "b"]


def test_probe_runner_dry_run_builds_no_watchers(monkeypatch) -> None:
    """Dry-run must not construct (let alone sweep) real watchers — they post
    real alerts to the bridge."""
    runner = _load_probe_runner()

    def boom():
        raise AssertionError("dry-run must not build watchers")

    monkeypatch.setenv("INFRA_PROBE_DRY_RUN", "1")
    monkeypatch.setattr(runner, "_build_watchers", boom)
    monkeypatch.setattr(runner, "run_once", lambda **_k: 0)
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--once", "--json"])

    assert runner.main() == 0


def test_probe_runner_touches_state_file_at_loop_start(monkeypatch, tmp_path) -> None:
    """Liveness-first for the LOCAL healthcheck (#543): the state file is
    refreshed at iteration start, so the freshness window measures loop
    liveness rather than iteration duration."""
    runner = _load_probe_runner()
    state = tmp_path / "state.json"
    touched: list[str] = []

    monkeypatch.setenv("INFRA_PROBE_STATE_FILE", str(state))
    monkeypatch.setattr(runner, "_build_watchers", lambda: [])
    monkeypatch.setattr(runner, "run_once", lambda **_k: 0)
    monkeypatch.setattr(runner, "_post_heartbeat", lambda **_k: None)
    monkeypatch.setattr(
        runner, "_touch_state", lambda path: touched.append(str(path))
    )
    monkeypatch.setattr(
        runner.time, "sleep", lambda _s: (_ for _ in ()).throw(SystemExit(0))
    )
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--loop", "--json"])

    try:
        runner.main()
    except SystemExit:
        pass

    assert touched == [str(state)]


# ---------------------------------------------------------------------------
# compose topology: ONE resident sidecar


def test_compose_has_single_resident_sidecar_with_watcher_env() -> None:
    """#543 operator decision locked in compose: the two watcher services are
    gone; the probe-runner service carries their env triads (continuity), the
    read-only docker socket, and the resource budget (ops.standards §5.2)."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert "container-breakdown-watch" not in services
    assert "deploy-queue-guard" not in services
    assert set(services) == {"vault-agent", "feishu-alert-bridge", "infra-probe-runner"}

    runner = services["infra-probe-runner"]
    env = runner["environment"]
    for key, default in {
        "ENV": "${ENV:-production}",
        "DEPLOY_GUARD_CEILING_SECONDS": "${DEPLOY_GUARD_CEILING_SECONDS:-1800}",
        "DEPLOY_GUARD_REMEDIATE": "${DEPLOY_GUARD_REMEDIATE:-0}",
        "DEPLOY_GUARD_RENOTIFY_SECONDS": "${DEPLOY_GUARD_RENOTIFY_SECONDS:-1800}",
        "BREAKDOWN_FAILURE_THRESHOLD": "${BREAKDOWN_FAILURE_THRESHOLD:-3}",
        "BREAKDOWN_RECOVERY_THRESHOLD": "${BREAKDOWN_RECOVERY_THRESHOLD:-5}",
        "BREAKDOWN_RENOTIFY_SECONDS": "${BREAKDOWN_RENOTIFY_SECONDS:-1800}",
    }.items():
        assert env.get(key) == default, f"{key}: {env.get(key)!r}"

    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in runner["volumes"]
    # §5.2 resource budget on the merged service
    assert runner["mem_limit"] == "${PROBE_RUNNER_MEM_LIMIT:-256m}"
    assert runner["cpu_shares"] == 512
    # the functional healthcheck (state-file freshness) survives the merge
    assert "infra_probe_runner_state.json" in runner["healthcheck"]["test"][1]


def test_deleted_sidecar_tools_stay_deleted() -> None:
    """The standalone entry points must not resurrect beside the plugins —
    that would be exactly the two-copies drift #543 collapsed."""
    assert not (ROOT / "tools/container_breakdown_watch.py").exists()
    assert not (ROOT / "tools/deploy_queue_guard.py").exists()
