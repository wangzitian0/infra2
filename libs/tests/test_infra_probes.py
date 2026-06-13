"""Tests for infra service probe contracts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import libs.infra_probes as probes
from libs.infra_probes import (
    build_probe_alert_payload,
    failed_results,
    parse_probe_specs,
    run_probe,
)


ROOT = Path(__file__).resolve().parents[2]


def _load_probe_runner():
    path = ROOT / "tools/infra_probe_runner.py"
    spec = importlib.util.spec_from_file_location("infra_probe_runner_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_specs_parse_http_tcp_and_command() -> None:
    """#183: probe config is code-owned and typed."""
    specs = parse_probe_specs(
        """
        vault-http|http|https://vault.example/v1/sys/health|200,429|critical|3
        postgres|tcp|platform-postgres:5432|connected|critical|5
        docker|command|docker info|Server|warning|10
        """
    )

    assert [spec.kind for spec in specs] == ["http", "tcp", "command"]
    assert specs[0].expected == "200,429"
    assert specs[2].timeout_seconds == 10


def test_http_probe_matches_expected_status() -> None:
    spec = parse_probe_specs("minio|http|https://minio.example/minio/health/live|200")[
        0
    ]

    result = run_probe(spec, http_get=lambda _url, _timeout: (200, "ok"))

    assert result.ok is True
    assert result.observed == "200:ok"


def test_tcp_and_command_probe_failures_are_classified() -> None:
    tcp = parse_probe_specs("redis|tcp|platform-redis:6379|connected")[0]
    tcp_result = run_probe(
        tcp,
        tcp_connect=lambda *_args: (_ for _ in ()).throw(OSError("refused")),
    )

    command = parse_probe_specs("docker|command|docker info|Server")[0]
    command_result = run_probe(
        command,
        command_runner=lambda *_args: subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="daemon down",
        ),
    )

    assert failed_results([tcp_result, command_result]) == [tcp_result, command_result]
    assert "refused" in tcp_result.summary
    assert "daemon down" in command_result.summary


def test_failed_probes_build_signoz_compatible_payload() -> None:
    spec = parse_probe_specs("vault|http|https://vault.example/v1/sys/health|200")[0]
    result = run_probe(spec, http_get=lambda *_args: (503, "sealed"))

    payload = build_probe_alert_payload([result])

    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "InfraServiceProbeFailed"
    assert payload["alerts"][0]["labels"]["service"] == "vault"
    assert payload["alerts"][0]["annotations"]["observed"] == "503:sealed"


def test_cloudflare_1010_failures_are_classified_as_probe_client_blocked() -> None:
    """#183: Cloudflare browser-signature blocks are not reported as service down."""
    spec = parse_probe_specs("signoz|http|https://signoz.example|200")[0]
    result = run_probe(spec, http_get=lambda *_args: (403, "error code: 1010"))

    payload = build_probe_alert_payload([result])

    assert payload["alerts"][0]["labels"]["failure_domain"] == "probe-client-blocked"
    assert payload["alerts"][0]["annotations"]["observed"] == "403:error code: 1010"


def test_http_probe_sends_stable_browser_compatible_headers(monkeypatch) -> None:
    """#183: public-route probes must not trip Cloudflare Browser Integrity Check."""
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"ok"

    def fake_urlopen(request, *, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(probes, "urlopen", fake_urlopen)
    spec = parse_probe_specs("route|http|https://cloud.example|200|critical|3")[0]

    result = probes.run_probe(spec)

    assert result.ok is True
    assert captured == {
        "user_agent": probes.HTTP_PROBE_HEADERS["User-Agent"],
        "accept": probes.HTTP_PROBE_HEADERS["Accept"],
        "timeout": 3.0,
    }


def test_probe_runner_loop_catches_iteration_errors(monkeypatch) -> None:
    """#183: looped runner keeps future probes alive after one failed iteration."""
    runner = _load_probe_runner()
    calls = []

    def fake_run_once(*, as_json, **_kwargs):
        calls.append(as_json)
        if len(calls) == 1:
            raise RuntimeError("bridge unavailable")
        return 0

    def fake_sleep(_seconds):
        if len(calls) >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--loop", "--json"])

    try:
        runner.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert calls == [True, True]


def test_probe_runner_defaults_to_fast_probe_bounded_notification() -> None:
    """#209: internal probes are fast, but notifications use thresholds."""
    runner = _load_probe_runner()
    compose = (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")

    assert runner.DEFAULT_PROBE_INTERVAL_SECONDS == 60
    assert runner.DEFAULT_FAILURE_THRESHOLD == 3
    assert runner.DEFAULT_RECOVERY_THRESHOLD == 2
    assert runner.DEFAULT_RENOTIFY_SECONDS == 1800
    assert (
        "INFRA_PROBE_INTERVAL_SECONDS: ${INFRA_PROBE_INTERVAL_SECONDS:-60}" in compose
    )
    assert (
        "INFRA_PROBE_FAILURE_THRESHOLD: ${INFRA_PROBE_FAILURE_THRESHOLD:-3}" in compose
    )
    assert (
        "INFRA_PROBE_RECOVERY_THRESHOLD: ${INFRA_PROBE_RECOVERY_THRESHOLD:-2}"
        in compose
    )
    assert (
        "INFRA_PROBE_RENOTIFY_SECONDS: ${INFRA_PROBE_RENOTIFY_SECONDS:-1800}" in compose
    )


def test_in_band_probe_compose_uses_internal_network_targets() -> None:
    """#183: in-band probes must not route through Cloudflare public domains."""
    compose = (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")
    probe_block = compose.split("INFRA_PROBE_SPECS: |", 1)[1].split(
        "PUBLIC_ROUTE_PROBE_SPECS:", 1
    )[0]

    assert "https://cloud.${INTERNAL_DOMAIN}" not in probe_block
    assert "https://vault.${INTERNAL_DOMAIN}" not in probe_block
    assert "https://minio.${INTERNAL_DOMAIN}" not in probe_block
    assert "https://sso.${INTERNAL_DOMAIN}" not in probe_block
    assert "https://signoz.${INTERNAL_DOMAIN}" not in probe_block
    assert "http://dokploy:3000" in probe_block
    assert "http://vault:8200/v1/sys/health" in probe_block
    assert "http://platform-minio${ENV_SUFFIX}:9000/minio/health/live" in probe_block
    assert (
        "http://platform-authentik-server${ENV_SUFFIX}:9000/-/health/live/"
        in probe_block
    )
    # signoz/clickhouse are prod_only (single shared instance — see
    # platform/{11.signoz,03.clickhouse}/deploy.py). They are probed WITHOUT
    # ${ENV_SUFFIX} from every env, matching openpanel. A -staging suffix would
    # target a phantom host and fire a permanent false-positive alert.
    assert "http://platform-signoz:8080/api/v1/health" in probe_block
    assert "http://platform-clickhouse:8123/ping" in probe_block
    assert "platform-signoz${ENV_SUFFIX}" not in probe_block
    assert "platform-clickhouse${ENV_SUFFIX}" not in probe_block


def test_public_route_probe_compose_is_not_enabled_by_default() -> None:
    """#209: public-route truth is primarily owned by Cloudflare."""
    compose = (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")

    assert "PUBLIC_ROUTE_PROBE_SPECS" not in compose


def test_probe_runner_dedupes_unchanged_failures_and_sends_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    """#183: repeated unchanged probe failures are quiet until recovery or renotify."""
    runner = _load_probe_runner()
    posted: list[dict] = []
    outcomes = [(503, "sealed"), (503, "sealed"), (200, "ok")]

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner,
        "post_alert_bridge_payload",
        lambda _url, payload, **_kwargs: posted.append(payload),
    )

    def fake_run_probes(specs):
        status, body = outcomes.pop(0)
        return [probes.run_probe(specs[0], http_get=lambda *_args: (status, body))]

    monkeypatch.setattr(runner, "run_probes", fake_run_probes)
    state_path = tmp_path / "probe-state.json"

    assert (
        runner.run_once(
            state_path=state_path,
            renotify_seconds=3600,
            failure_threshold=1,
            recovery_threshold=1,
        )
        == 1
    )
    assert (
        runner.run_once(
            state_path=state_path,
            renotify_seconds=3600,
            failure_threshold=1,
            recovery_threshold=1,
        )
        == 1
    )
    assert (
        runner.run_once(
            state_path=state_path,
            renotify_seconds=3600,
            failure_threshold=1,
            recovery_threshold=1,
        )
        == 0
    )

    assert [payload["status"] for payload in posted] == ["firing", "resolved"]
    assert posted[0]["commonLabels"]["alertname"] == "InfraServiceProbeFailed"
    assert posted[1]["commonLabels"]["alertname"] == "InfraServiceProbeFailed"


def test_probe_runner_renotifies_after_interval(monkeypatch, tmp_path) -> None:
    """#183: unresolved failures renotify only after the configured interval."""
    runner = _load_probe_runner()
    posted: list[dict] = []

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner,
        "post_alert_bridge_payload",
        lambda _url, payload, **_kwargs: posted.append(payload),
    )
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [
            probes.run_probe(specs[0], http_get=lambda *_args: (503, "sealed"))
        ],
    )
    timestamps = iter([100.0, 699.0, 701.0])
    monkeypatch.setattr(runner.time, "time", lambda: next(timestamps))
    state_path = tmp_path / "probe-state.json"

    assert (
        runner.run_once(
            state_path=state_path,
            renotify_seconds=600,
            failure_threshold=1,
            recovery_threshold=1,
        )
        == 1
    )
    assert (
        runner.run_once(
            state_path=state_path,
            renotify_seconds=600,
            failure_threshold=1,
            recovery_threshold=1,
        )
        == 1
    )
    assert (
        runner.run_once(
            state_path=state_path,
            renotify_seconds=600,
            failure_threshold=1,
            recovery_threshold=1,
        )
        == 1
    )

    assert [payload["status"] for payload in posted] == ["firing", "firing"]


def test_probe_runner_posts_public_route_alerts_separately(
    monkeypatch, tmp_path
) -> None:
    """#183: public-route failures have a distinct alert name and state bucket."""
    runner = _load_probe_runner()
    posted: list[dict] = []

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv(
        "PUBLIC_ROUTE_PROBE_SPECS",
        "vault-public-route|http|https://vault.example/v1/sys/health|200|warning|5",
    )
    monkeypatch.setattr(
        runner,
        "post_alert_bridge_payload",
        lambda _url, payload, **_kwargs: posted.append(payload),
    )

    def fake_run_probes(specs):
        spec = specs[0]
        if "public-route" in spec.name:
            return [probes.run_probe(spec, http_get=lambda *_args: (521, "down"))]
        return [probes.run_probe(spec, http_get=lambda *_args: (200, "ok"))]

    monkeypatch.setattr(runner, "run_probes", fake_run_probes)

    assert (
        runner.run_once(
            state_path=tmp_path / "probe-state.json",
            failure_threshold=1,
            recovery_threshold=1,
        )
        == 1
    )

    assert len(posted) == 1
    assert posted[0]["commonLabels"]["alertname"] == "InfraPublicRouteProbeFailed"
    assert posted[0]["commonLabels"]["severity"] == "warning"


def test_probe_runner_requires_consecutive_failures_before_alerting(
    monkeypatch,
    tmp_path,
) -> None:
    """#209: fast internal probing must not produce alert noise on one failure."""
    runner = _load_probe_runner()
    posted: list[dict] = []

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner,
        "post_alert_bridge_payload",
        lambda _url, payload, **_kwargs: posted.append(payload),
    )
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [
            probes.run_probe(specs[0], http_get=lambda *_args: (503, "sealed"))
        ],
    )
    state_path = tmp_path / "probe-state.json"

    assert runner.run_once(state_path=state_path, failure_threshold=3) == 1
    assert runner.run_once(state_path=state_path, failure_threshold=3) == 1
    assert posted == []
    assert runner.run_once(state_path=state_path, failure_threshold=3) == 1

    assert [payload["status"] for payload in posted] == ["firing"]


def test_probe_runner_requires_consecutive_recoveries_before_resolving(
    monkeypatch,
    tmp_path,
) -> None:
    """#209: one passing probe should not resolve a flapping incident."""
    runner = _load_probe_runner()
    posted: list[dict] = []
    outcomes = [(503, "down"), (503, "down"), (200, "ok"), (200, "ok")]

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner,
        "post_alert_bridge_payload",
        lambda _url, payload, **_kwargs: posted.append(payload),
    )

    def fake_run_probes(specs):
        status, body = outcomes.pop(0)
        return [probes.run_probe(specs[0], http_get=lambda *_args: (status, body))]

    monkeypatch.setattr(runner, "run_probes", fake_run_probes)
    state_path = tmp_path / "probe-state.json"

    assert (
        runner.run_once(
            state_path=state_path,
            failure_threshold=2,
            recovery_threshold=2,
        )
        == 1
    )
    assert (
        runner.run_once(
            state_path=state_path,
            failure_threshold=2,
            recovery_threshold=2,
        )
        == 1
    )
    assert (
        runner.run_once(
            state_path=state_path,
            failure_threshold=2,
            recovery_threshold=2,
        )
        == 0
    )
    assert [payload["status"] for payload in posted] == ["firing"]
    assert (
        runner.run_once(
            state_path=state_path,
            failure_threshold=2,
            recovery_threshold=2,
        )
        == 0
    )

    assert [payload["status"] for payload in posted] == ["firing", "resolved"]


def test_probe_runner_posts_cloudflare_watchdog_heartbeat(
    monkeypatch, tmp_path
) -> None:
    """Infra-011.2: the external watchdog can detect a stopped probe runner."""
    runner = _load_probe_runner()
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"ok"

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["accept"] = request.get_header("Accept")
        captured["content_type"] = request.get_header("Content-type")
        captured["timeout"] = timeout
        captured["user_agent"] = request.get_header("User-agent")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setenv(
        "INFRA_PROBE_HEARTBEAT_URL", "https://watchdog.example/heartbeat"
    )
    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_TOKEN", "heartbeat-token")
    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_ENV", "staging")
    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_NAME", "platform-alerting-probes-staging")
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [probes.run_probe(specs[0], http_get=lambda *_args: (200, "ok"))],
    )
    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    monkeypatch.setattr(runner.time, "time", lambda: 12345)

    assert runner.run_once(state_path=tmp_path / "probe-state.json") == 0

    assert captured == {
        "url": "https://watchdog.example/heartbeat",
        "accept": probes.HTTP_PROBE_HEADERS["Accept"],
        "authorization": "Bearer heartbeat-token",
        "content_type": "application/json",
        "timeout": 5.0,
        "user_agent": probes.HTTP_PROBE_HEADERS["User-Agent"],
        "payload": {
            "detail": "probe loop completed",
            "env": "staging",
            "name": "platform-alerting-probes-staging",
            "ok": True,
            "timestamp": 12345,
        },
    }


def test_probe_runner_heartbeat_is_configured_in_alerting_compose() -> None:
    """Infra-011.2: prod/staging alerting deployments can publish heartbeats."""
    compose = (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")

    assert "INFRA_PROBE_HEARTBEAT_URL: ${INFRA_PROBE_HEARTBEAT_URL:-}" in compose
    assert "INFRA_PROBE_HEARTBEAT_TOKEN: ${INFRA_PROBE_HEARTBEAT_TOKEN:-}" in compose
    assert "INFRA_PROBE_HEARTBEAT_ENV: ${ENV:-production}" in compose
    assert (
        "INFRA_PROBE_HEARTBEAT_NAME: platform-alerting-probes${ENV_SUFFIX}" in compose
    )


def test_probe_runner_env_file_overrides_empty_compose_defaults(
    monkeypatch, tmp_path
) -> None:
    """Infra-011.2: heartbeat secrets can come from Vault-rendered /secrets/.env."""
    runner = _load_probe_runner()
    env_file = tmp_path / ".env"
    env_file.write_text(
        'INFRA_PROBE_HEARTBEAT_URL="https://watchdog.example/heartbeat"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_URL", "")

    runner._load_env_file(env_file)

    assert (
        runner.os.environ["INFRA_PROBE_HEARTBEAT_URL"]
        == "https://watchdog.example/heartbeat"
    )


def test_resource_probe_threshold_pass_fail() -> None:
    """A `resource` probe passes while usage <= the % ceiling, fails above it."""
    spec = parse_probe_specs("host-cpu|resource|cpu|80")[0]
    assert probes._matches_expected(spec, "12.5") is True
    assert probes._matches_expected(spec, "80.0") is True
    assert probes._matches_expected(spec, "80.1") is False
    assert probes._matches_expected(spec, "97") is False
    # non-numeric observed must fail closed
    assert probes._matches_expected(spec, "ValueError") is False


def test_resource_disk_probe_returns_percent() -> None:
    """disk:<path> reports a 0-100 usage percentage (statvfs, portable)."""
    spec = parse_probe_specs("host-disk|resource|disk:/|80")[0]
    observed = probes._run_resource(spec)
    value = float(observed)
    assert 0.0 <= value <= 100.0


def test_resource_unknown_target_raises() -> None:
    spec = parse_probe_specs("bad|resource|gpu|80")[0]
    try:
        probes._run_resource(spec)
    except ValueError as exc:
        assert "gpu" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown resource target")


def test_cpu_and_mem_percent_when_proc_available() -> None:
    """On Linux (/proc present) cpu/mem report a 0-100 percentage."""
    import os

    if not os.path.exists("/proc/stat") or not os.path.exists("/proc/meminfo"):
        return  # /proc not available (e.g. macOS dev box) — covered on Linux CI
    cpu = float(probes._run_resource(parse_probe_specs("c|resource|cpu|80")[0]))
    mem = float(probes._run_resource(parse_probe_specs("m|resource|mem|80")[0]))
    assert 0.0 <= cpu <= 100.0
    assert 0.0 <= mem <= 100.0


def test_host_resource_specs_gated_to_production(monkeypatch) -> None:
    """Resource probes run on the production runner only (shared host)."""
    runner = _load_probe_runner()
    specs = parse_probe_specs(
        "vault|http|http://vault|200\nhost-cpu|resource|cpu|80"
    )

    # the runner sets INFRA_PROBE_HEARTBEAT_ENV (not always ENV) — gate on it.
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("DEPLOY_ENV", raising=False)

    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_ENV", "production")
    assert {s.kind for s in runner._host_specs_for_env(specs)} == {"http", "resource"}

    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_ENV", "staging")
    assert {s.kind for s in runner._host_specs_for_env(specs)} == {"http"}

    # case-insensitive + ENV fallback when the heartbeat var is unset
    monkeypatch.delenv("INFRA_PROBE_HEARTBEAT_ENV", raising=False)
    monkeypatch.setenv("ENV", "Staging")
    assert {s.kind for s in runner._host_specs_for_env(specs)} == {"http"}
