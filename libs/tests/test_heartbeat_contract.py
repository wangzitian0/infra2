"""Heartbeat contract v2 end to end: the real runner's POST into the real Worker.

The Worker stands down its entrypoint page only for a route the VPS says it paged
(#903 `failing_public_routes`: failing now AND covered by a delivered, still-open
page) and only if that delivery is recent enough (#904). Each case below runs
tools/infra_probe_runner.py's own `run_once` with a scripted probe and bridge,
captures the heartbeat it POSTs, and replays that exact body into
cloudflare/infra-watchdog/worker.js (fixtures/watchdog_contract_harness.mjs),
which then sees the same route unreachable from Cloudflare for two runs.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

import libs.observability.probes as probes

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "cloudflare/infra-watchdog/worker.js"
WRANGLER = ROOT / "cloudflare/infra-watchdog/wrangler.toml"
HARNESS = Path(__file__).resolve().parent / "fixtures/watchdog_contract_harness.mjs"
NODE = shutil.which("node")
ROUTE = "finance-report-web-public-route"
NOW = 1_790_208_000.0

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit):
        return b"ok"


def _runner_beat(
    monkeypatch, tmp_path, *, threshold: int, bridge_fails: bool, maintenance: bool
) -> dict:
    spec = importlib.util.spec_from_file_location(
        f"infra_probe_runner_contract_{threshold}_{bridge_fails}_{maintenance}",
        ROOT / "tools/infra_probe_runner.py",
    )
    runner = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(runner)
    beats: list[dict] = []
    for name in ("INFRA_PROBE_DRY_RUN", "INFRA_PROBE_RENOTIFY_SECONDS", "DEPLOY_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_ENV", "production")
    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_NAME", "platform-alerting-probes")
    monkeypatch.setenv(
        "INFRA_PROBE_HEARTBEAT_URL", "https://watchdog.example/heartbeat"
    )
    monkeypatch.setenv("INFRA_PROBE_LEDGER_FILE", str(tmp_path / "ledger.json"))
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200|critical|5")
    monkeypatch.setenv(
        "PUBLIC_ROUTE_PROBE_SPECS",
        f"{ROUTE}|http|https://report.example/|200|critical|10",
    )
    monkeypatch.delenv("INFRA_PROBE_MAINTENANCE_UNTIL", raising=False)

    def fake_post(_url, payload, **_kwargs):
        if bridge_fails:
            raise OSError("bridge unreachable")
        return {}

    def fake_run_probes(specs):
        return [
            probes.ProbeResult(
                spec=s,
                ok=s.name != ROUTE,
                summary="ok" if s.name != ROUTE else "expected '200', observed '503'",
                observed="ok" if s.name != ROUTE else "503",
                elapsed_ms=1,
            )
            for s in specs
        ]

    def fake_urlopen(request, *, timeout):
        beats.append(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(runner, "post_alert_bridge_payload", fake_post)
    monkeypatch.setattr(runner, "run_probes", fake_run_probes)
    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    clock = {"now": NOW}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    runner.run_once(state_path=tmp_path / "state.json", failure_threshold=threshold)
    if maintenance:
        # the route was paged and is still open when maintenance begins
        monkeypatch.setenv("INFRA_PROBE_MAINTENANCE_UNTIL", str(NOW + 3600))
        clock["now"] = NOW + 60
        runner.run_once(state_path=tmp_path / "state.json", failure_threshold=threshold)
    verdicts = [beat for beat in beats if not beat.get("liveness")]
    assert verdicts, "the runner posted no verdict heartbeat"
    return {**verdicts[-1], "failing_route_under_test": [ROUTE]}


@pytest.fixture(scope="module")
def contract(tmp_path_factory) -> dict:
    work = tmp_path_factory.mktemp("contract")
    monkeypatch = pytest.MonkeyPatch()
    cases = {
        # paged by the VPS: failing, past its debounce, delivered, still open
        "paged": dict(threshold=1, bridge_fails=False, maintenance=False),
        # failing, but below the runner's debounce: nothing paged yet
        "notYetPaged": dict(threshold=3, bridge_fails=False, maintenance=False),
        # the page could not be delivered
        "undelivered": dict(threshold=1, bridge_fails=True, maintenance=False),
        # paged, then maintenance began: the runner lists nothing while it is on
        "maintenance": dict(threshold=1, bridge_fails=False, maintenance=True),
    }
    beats = {}
    try:
        for label, options in cases.items():
            case_dir = work / label
            case_dir.mkdir()
            beats[label] = _runner_beat(monkeypatch, case_dir, **options)
    finally:
        monkeypatch.undo()
    module = work / "worker.mjs"
    module.write_text(WORKER.read_text(encoding="utf-8"), encoding="utf-8")
    variables = work / "vars.json"
    variables.write_text(
        json.dumps(tomllib.loads(WRANGLER.read_text(encoding="utf-8"))["vars"]),
        encoding="utf-8",
    )
    beats_file = work / "beats.json"
    beats_file.write_text(json.dumps(beats), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(HARNESS), str(module), str(variables), str(beats_file)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {"beats": beats, "worker": json.loads(result.stdout)}


def test_the_runner_speaks_contract_v2(contract) -> None:
    paged = contract["beats"]["paged"]
    assert paged["schema"] == 2
    assert paged["failing_public_routes"] == [ROUTE]
    assert paged["last_delivery_ok_at"] == int(NOW)
    stored = contract["worker"]["paged"]
    assert stored["status"] == 200
    assert stored["storedRoutes"] == [ROUTE]
    assert stored["storedDeliveryAt"] == int(NOW)


def test_a_route_the_vps_paged_is_not_paged_again_by_the_worker(contract) -> None:
    assert contract["worker"]["paged"]["entrypointPages"] == 0


@pytest.mark.parametrize("case", ["notYetPaged", "undelivered", "maintenance"])
def test_the_worker_pages_what_the_vps_did_not(contract, case) -> None:
    assert contract["beats"][case]["failing_public_routes"] == []
    assert contract["worker"][case]["entrypointPages"] == 1


def test_an_undelivered_page_is_reported_as_an_unhealthy_loop(contract) -> None:
    """The runner reports ok=false while its page is undelivered; both Worker runs
    read that verdict, so the loop P1 fires on the second, alongside the entrypoint
    page the VPS could not deliver."""
    assert contract["beats"]["undelivered"]["ok"] is False
    assert "bridge delivery failed" in contract["beats"]["undelivered"]["detail"]
    assert contract["worker"]["undelivered"]["loopPages"] == 1
    assert contract["worker"]["paged"]["loopPages"] == 0
