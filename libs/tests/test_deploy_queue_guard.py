"""Tests for the deploy-queue guard watcher (libs/deploy_queue_guard.py).

The pure stuck-detection logic is covered by test_deploy_queue.py; this file
covers the watcher's orchestration: env-file loading semantics, the compose
sweep's failure isolation, alert delivery guards, the renotify suppression
window, and the remediate/escalate sequence. Since the #543 single-sidecar
merge the guard is a ResidentWatcher plugin inside tools/infra_probe_runner.py
rather than a standalone sidecar — these tests are the behavior-parity proof
that the merge changed the packaging, not the semantics.
"""

from __future__ import annotations

from pathlib import Path


from libs.deploy_queue import StuckDeploy
from libs import deploy_queue_guard as guard


# ---------------------------------------------------------------------------
# _load_env_file


def test_load_env_file_skips_empty_values(tmp_path: Path, monkeypatch) -> None:
    """An env file rendered before Vault populated a secret (KEY="") must not
    poison os.environ — the next non-empty render must win."""
    env_file = tmp_path / ".env"
    env_file.write_text('DOKPLOY_API_KEY=""\nGOOD_KEY="value"\n', encoding="utf-8")
    monkeypatch.delenv("DOKPLOY_API_KEY", raising=False)
    monkeypatch.delenv("GOOD_KEY", raising=False)

    guard._load_env_file(env_file)

    import os

    assert "DOKPLOY_API_KEY" not in os.environ
    assert os.environ.get("GOOD_KEY") == "value"
    monkeypatch.delenv("GOOD_KEY", raising=False)


def test_load_env_file_does_not_override_existing(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('MY_KEY="from-file"\n', encoding="utf-8")
    monkeypatch.setenv("MY_KEY", "already-set")

    guard._load_env_file(env_file)

    import os

    assert os.environ["MY_KEY"] == "already-set"


def test_load_env_file_missing_file_is_noop(tmp_path: Path) -> None:
    guard._load_env_file(tmp_path / "does-not-exist.env")


def test_load_env_file_ignores_comments_and_garbage(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nnot-an-assignment\nK='v'\n", encoding="utf-8")
    monkeypatch.delenv("K", raising=False)

    guard._load_env_file(env_file)

    import os

    assert os.environ.get("K") == "v"
    monkeypatch.delenv("K", raising=False)


# ---------------------------------------------------------------------------
# _env_int


def test_env_int_defaults_and_parses(monkeypatch) -> None:
    monkeypatch.delenv("X_INT", raising=False)
    assert guard._env_int("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "42")
    assert guard._env_int("X_INT", 7) == 42
    monkeypatch.setenv("X_INT", "not-a-number")
    assert guard._env_int("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "")
    assert guard._env_int("X_INT", 7) == 7


# ---------------------------------------------------------------------------
# _list_composes — one compose's failure must not abort the sweep


class _FakeClient:
    def __init__(self, projects, deployments_by_compose, fail_for=()):
        self._projects = projects
        self._deployments = deployments_by_compose
        self._fail_for = set(fail_for)
        self.killed: list[str] = []
        self.cancelled: list[str] = []
        self.cleaned: list[str] = []

    def list_projects(self):
        return self._projects

    def get_compose_deployments(self, compose_id):
        if compose_id in self._fail_for:
            raise RuntimeError("dokploy 500")
        return self._deployments.get(compose_id, [])

    def kill_compose_build(self, compose_id):
        self.killed.append(compose_id)

    def cancel_compose_deployment(self, compose_id):
        self.cancelled.append(compose_id)

    def clean_compose_queues(self, compose_id):
        self.cleaned.append(compose_id)


def _projects(*composes):
    return [
        {
            "name": "platform",
            "environments": [{"name": "production", "compose": list(composes)}],
        }
    ]


def test_list_composes_isolates_per_compose_failure() -> None:
    client = _FakeClient(
        projects=_projects(
            {"composeId": "c1", "name": "svc-one"},
            {"composeId": "c2", "name": "svc-two"},
            {"name": "no-id-skipped"},
        ),
        deployments_by_compose={"c2": [{"status": "running"}]},
        fail_for={"c1"},
    )

    out = guard._list_composes(client)

    assert [(item.compose_id, item.compose_name) for item in out] == [
        ("c1", "svc-one"),
        ("c2", "svc-two"),
    ]
    assert out[0].deployments == ()  # failed fetch degrades to empty
    assert out[1].deployments == ({"status": "running"},)


# ---------------------------------------------------------------------------
# _post_alert


def test_post_alert_without_bridge_url_does_not_raise(monkeypatch) -> None:
    monkeypatch.delenv("ALERT_BRIDGE_URL", raising=False)
    guard._post_alert({"status": "firing"})


def test_post_alert_passes_basic_auth(monkeypatch) -> None:
    sent = {}

    def fake_post(url, payload, username="", password=""):
        sent.update(url=url, payload=payload, username=username, password=password)

    monkeypatch.setenv("ALERT_BRIDGE_URL", "http://bridge:8080/signoz/webhook")
    monkeypatch.setenv("BRIDGE_BASIC_AUTH_USERNAME", "u")
    monkeypatch.setenv("BRIDGE_BASIC_AUTH_PASSWORD", "p")
    import libs.infra_probes

    monkeypatch.setattr(libs.infra_probes, "post_alert_bridge_payload", fake_post)

    guard._post_alert({"status": "firing"})

    assert sent["url"] == "http://bridge:8080/signoz/webhook"
    assert (sent["username"], sent["password"]) == ("u", "p")


def test_post_alert_delivery_failure_does_not_crash_loop(monkeypatch) -> None:
    monkeypatch.setenv("ALERT_BRIDGE_URL", "http://bridge:8080/signoz/webhook")
    import libs.infra_probes

    def boom(*a, **k):
        raise RuntimeError("bridge down")

    monkeypatch.setattr(libs.infra_probes, "post_alert_bridge_payload", boom)

    guard._post_alert({"status": "firing"})  # must not raise


# ---------------------------------------------------------------------------
# run_once — alerting, renotify suppression, remediate flag


def _stuck_compose(compose_id="c1", name="svc", age_seconds=3600):
    """A compose whose single deployment has been running `age_seconds`."""
    import time

    started = time.time() - age_seconds
    return {
        "composeId": compose_id,
        "name": name,
    }, {compose_id: [{"status": "running", "deploymentId": "d1", "startedAt": started}]}


def test_run_once_no_stuck_returns_zero(monkeypatch) -> None:
    client = _FakeClient(
        projects=_projects({"composeId": "c1", "name": "svc"}),
        deployments_by_compose={},
    )
    alerts: list[dict] = []
    monkeypatch.setattr(guard, "_post_alert", alerts.append)

    count = guard.run_once(client, ceiling=1800, remediate=False, grace=0, alerted={})

    assert count == 0
    assert alerts == []


def test_run_once_alerts_on_stuck_and_observe_only_by_default(monkeypatch) -> None:
    compose, deployments = _stuck_compose(age_seconds=3600)
    client = _FakeClient(
        projects=_projects(compose), deployments_by_compose=deployments
    )
    alerts: list[dict] = []
    monkeypatch.setattr(guard, "_post_alert", alerts.append)

    count = guard.run_once(client, ceiling=1800, remediate=False, grace=0, alerted={})

    assert count == 1
    assert len(alerts) == 1
    assert alerts[0]["commonLabels"]["alertname"] == "DeployQueueStuck"
    assert client.killed == []  # observe-only: no remediation calls


def test_run_once_suppresses_renotify_within_window(monkeypatch) -> None:
    compose, deployments = _stuck_compose(age_seconds=3600)
    client = _FakeClient(
        projects=_projects(compose), deployments_by_compose=deployments
    )
    alerts: list[dict] = []
    monkeypatch.setattr(guard, "_post_alert", alerts.append)
    monkeypatch.setenv("DEPLOY_GUARD_RENOTIFY_SECONDS", "1800")

    alerted: dict[str, float] = {}
    guard.run_once(client, ceiling=1800, remediate=False, grace=0, alerted=alerted)
    guard.run_once(client, ceiling=1800, remediate=False, grace=0, alerted=alerted)

    assert len(alerts) == 1  # second sweep inside the window stays silent


def test_run_once_remediate_kills_then_escalates_if_still_running(monkeypatch) -> None:
    compose, deployments = _stuck_compose(age_seconds=3600)
    client = _FakeClient(
        projects=_projects(compose), deployments_by_compose=deployments
    )
    alerts: list[dict] = []
    monkeypatch.setattr(guard, "_post_alert", alerts.append)
    monkeypatch.setenv("DEPLOY_GUARD_CEILING_SECONDS", "1800")

    guard.run_once(client, ceiling=1800, remediate=True, grace=0, alerted={})

    # kill sequence went through Dokploy's API, never Redis
    assert client.killed == ["c1"]
    assert client.cancelled == ["c1"]
    assert client.cleaned == ["c1"]
    # deployment still 'running' after the kill (client state unchanged) -> escalation alert
    descriptions = [
        a["alerts"][0]["annotations"]["description"] for a in alerts if a.get("alerts")
    ]
    assert any("manual intervention" in d or "STILL running" in d for d in descriptions)


def test_remediate_survives_kill_api_failure(monkeypatch) -> None:
    compose, deployments = _stuck_compose(age_seconds=3600)
    client = _FakeClient(
        projects=_projects(compose), deployments_by_compose=deployments
    )

    def kill_boom(compose_id):
        raise RuntimeError("dokploy 500")

    client.kill_compose_build = kill_boom
    monkeypatch.setattr(guard, "_post_alert", lambda payload: None)

    stuck = [StuckDeploy("c1", "svc", "d1", 3600.0)]
    guard._remediate(client, stuck, grace_seconds=0)  # must not raise

    assert client.cancelled == ["c1"]  # later steps still attempted


# ---------------------------------------------------------------------------
# DeployQueueGuard watcher plugin (#543) — the standalone `main --once/--loop`
# entry is gone; the same behaviors now hang off the plugin's sweep.


def test_watcher_sweep_survives_client_unavailable(monkeypatch, tmp_path: Path) -> None:
    """The pre-merge loop idled (never crashlooped) while DOKPLOY_API_KEY was
    still unrendered; the plugin must keep that: a client construction failure
    is logged, never raised into the probe loop."""
    watcher = guard.DeployQueueGuard(
        {"ALERTING_ENV_FILE": str(tmp_path / "missing.env")}
    )

    def no_client():
        raise RuntimeError("DOKPLOY_API_KEY missing")

    monkeypatch.setattr(guard, "_make_client", no_client)

    assert watcher.maybe_run(now=100.0) is True  # swept (and failed) quietly


def test_watcher_sweep_alerts_on_stuck(monkeypatch, tmp_path: Path) -> None:
    compose, deployments = _stuck_compose(age_seconds=7200)
    client = _FakeClient(
        projects=_projects(compose), deployments_by_compose=deployments
    )
    monkeypatch.setattr(guard, "_make_client", lambda: client)
    alerts: list[dict] = []
    monkeypatch.setattr(guard, "_post_alert", alerts.append)

    watcher = guard.DeployQueueGuard(
        {"ALERTING_ENV_FILE": str(tmp_path / "missing.env")}
    )
    watcher.maybe_run(now=100.0)

    assert len(alerts) == 1
    assert alerts[0]["commonLabels"]["alertname"] == "DeployQueueStuck"
    assert client.killed == []  # observe-only by default


def test_watcher_env_triad_maps_into_config_for_continuity() -> None:
    """#543: the historical DEPLOY_GUARD_* env names keep working, mapped into
    per-watcher config (the compose env block passes them through verbatim)."""
    watcher = guard.DeployQueueGuard(
        {
            "DEPLOY_GUARD_CEILING_SECONDS": "900",
            "DEPLOY_GUARD_INTERVAL_SECONDS": "120",
            "DEPLOY_GUARD_GRACE_SECONDS": "5",
            "DEPLOY_GUARD_REMEDIATE": "1",
            "ALERTING_ENV_FILE": "/nonexistent/.env",
        }
    )

    assert watcher.name == "deploy-queue-guard"
    assert watcher.ceiling == 900
    assert watcher.interval_seconds == 120
    assert watcher.grace == 5
    assert watcher.remediate is True

    defaults = guard.DeployQueueGuard({})
    assert defaults.ceiling == guard.DEFAULT_CEILING
    assert defaults.interval_seconds == guard.DEFAULT_INTERVAL
    assert defaults.remediate is False


def test_watcher_renotify_state_survives_across_sweeps(
    monkeypatch, tmp_path: Path
) -> None:
    """The `alerted` renotify map lives on the plugin instance for the sidecar
    process's lifetime — two sweeps inside the window page once (the standalone
    loop's exact suppression behavior)."""
    compose, deployments = _stuck_compose(age_seconds=7200)
    client = _FakeClient(
        projects=_projects(compose), deployments_by_compose=deployments
    )
    monkeypatch.setattr(guard, "_make_client", lambda: client)
    monkeypatch.setenv("DEPLOY_GUARD_RENOTIFY_SECONDS", "1800")
    alerts: list[dict] = []
    monkeypatch.setattr(guard, "_post_alert", alerts.append)

    watcher = guard.DeployQueueGuard(
        {"ALERTING_ENV_FILE": str(tmp_path / "missing.env")}
    )
    watcher.maybe_run(now=100.0)
    watcher.maybe_run(now=200.0)

    assert len(alerts) == 1  # second sweep inside the renotify window is silent


def test_watcher_reloads_env_file_each_sweep_for_late_rendered_key(
    monkeypatch, tmp_path: Path
) -> None:
    """A DOKPLOY_API_KEY that Vault renders AFTER the sidecar started must be
    picked up on a later sweep — including when the merged runner's startup
    env-file load already planted the key as an EMPTY value in os.environ."""
    import os

    env_file = tmp_path / ".env"
    env_file.write_text('LATE_KEY=""\n', encoding="utf-8")
    monkeypatch.setenv("LATE_KEY", "")  # startup load saw the empty render
    monkeypatch.setattr(guard, "_make_client", lambda: _FakeClient([], {}))

    watcher = guard.DeployQueueGuard({"ALERTING_ENV_FILE": str(env_file)})
    watcher.maybe_run(now=100.0)
    assert os.environ.get("LATE_KEY") == ""  # still empty: file value empty too

    env_file.write_text('LATE_KEY="rendered-now"\n', encoding="utf-8")
    watcher.maybe_run(now=100.0 + watcher.interval_seconds)
    assert os.environ.get("LATE_KEY") == "rendered-now"
