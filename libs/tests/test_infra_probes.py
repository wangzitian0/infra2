"""Tests for infra service probe contracts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from infra2_sdk.runtime.probes import DependencyStatus
from infra2_sdk.runtime.probes import ProbeResult as SdkProbeResult

import libs.observability.probes as probes
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


def test_postgres_probe_runs_a_real_select_1_not_just_a_tcp_handshake(
    monkeypatch,
) -> None:
    """kind="postgres" must prove Postgres itself is accepting queries — a bare TCP
    connect (the old platform-postgres-tcp probe) only proves the port is open."""
    monkeypatch.setenv("PROBE_POSTGRES_USER", "probe_monitor")
    monkeypatch.setenv("PROBE_POSTGRES_PASSWORD", "s3cret")
    spec = parse_probe_specs(
        "platform-postgres-select1|postgres|platform-postgres:5432/postgres"
    )[0]
    seen_settings = {}

    def fake_prober(settings):
        seen_settings["dsn"] = settings.dsn
        return SdkProbeResult(
            "database", DependencyStatus.PRESENT, "SELECT 1 succeeded", 4.0
        )

    result = run_probe(spec, postgres_prober=fake_prober)

    assert result.ok is True
    assert result.observed == "SELECT 1 succeeded"
    assert (
        seen_settings["dsn"]
        == "postgresql://probe_monitor:s3cret@platform-postgres:5432/postgres"
    )


def test_postgres_probe_fails_closed_without_password(monkeypatch) -> None:
    monkeypatch.delenv("PROBE_POSTGRES_PASSWORD", raising=False)
    spec = parse_probe_specs(
        "platform-postgres-select1|postgres|platform-postgres:5432/postgres"
    )[0]

    result = run_probe(
        spec,
        postgres_prober=lambda _settings: (_ for _ in ()).throw(
            AssertionError("must not connect without a password")
        ),
    )

    assert result.ok is False
    assert "PROBE_POSTGRES_PASSWORD" in result.summary


def test_postgres_probe_reports_the_sdk_detail_on_absence(monkeypatch) -> None:
    monkeypatch.setenv("PROBE_POSTGRES_PASSWORD", "s3cret")
    spec = parse_probe_specs(
        "platform-postgres-select1|postgres|platform-postgres:5432/postgres"
    )[0]

    result = run_probe(
        spec,
        postgres_prober=lambda _settings: SdkProbeResult(
            "database",
            DependencyStatus.ABSENT,
            "OperationalError: connection refused",
            4.0,
        ),
    )

    assert result.ok is False
    assert "connection refused" in result.summary


def test_s3_probe_runs_a_real_head_bucket_not_just_an_http_200(monkeypatch) -> None:
    """kind="s3" must prove the S3 API path works — the existing minio-internal-http
    probe only proves the process answers its liveness endpoint."""
    monkeypatch.setenv("PROBE_S3_BUCKET", "infra-probe-healthcheck")
    monkeypatch.setenv("PROBE_S3_ACCESS_KEY", "AKIA_PROBE")
    monkeypatch.setenv("PROBE_S3_SECRET_KEY", "s3cret")
    spec = parse_probe_specs("minio-s3-head-bucket|s3|http://platform-minio:9000")[0]
    seen_settings = {}

    def fake_prober(settings):
        seen_settings["bucket"] = settings.bucket
        seen_settings["endpoint_url"] = settings.endpoint_url
        return SdkProbeResult(
            "object_storage", DependencyStatus.PRESENT, "bucket accessible", 6.0
        )

    result = run_probe(spec, s3_prober=fake_prober)

    assert result.ok is True
    assert result.observed == "bucket accessible"
    assert seen_settings["bucket"] == "infra-probe-healthcheck"
    assert seen_settings["endpoint_url"] == "http://platform-minio:9000"


def test_s3_probe_fails_closed_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("PROBE_S3_BUCKET", raising=False)
    monkeypatch.delenv("PROBE_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("PROBE_S3_SECRET_KEY", raising=False)
    spec = parse_probe_specs("minio-s3-head-bucket|s3|http://platform-minio:9000")[0]

    result = run_probe(
        spec,
        s3_prober=lambda _settings: (_ for _ in ()).throw(
            AssertionError("must not connect without credentials")
        ),
    )

    assert result.ok is False
    assert "PROBE_S3_BUCKET" in result.summary


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
    monkeypatch.setattr(runner, "_build_watchers", lambda: [])
    monkeypatch.setattr(runner, "_post_heartbeat", lambda **_k: None)
    monkeypatch.setattr(runner, "_touch_state", lambda _p: None)
    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--loop", "--json"])

    try:
        runner.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert calls == [True, True]


def test_probe_runner_defaults_to_fast_probe_bounded_notification() -> None:
    """#209: internal probes are fast, but notifications use thresholds.

    #903: no timer re-notify (0) — an unchanged failing set is sent once."""
    runner = _load_probe_runner()
    compose = (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")

    assert runner.DEFAULT_PROBE_INTERVAL_SECONDS == 60
    assert runner.DEFAULT_FAILURE_THRESHOLD == 3
    assert runner.DEFAULT_RECOVERY_THRESHOLD == 2
    assert runner.DEFAULT_RENOTIFY_SECONDS == 0
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
    assert "INFRA_PROBE_RENOTIFY_SECONDS: ${INFRA_PROBE_RENOTIFY_SECONDS:-0}" in compose
    # #726: the never-passed grace period, same defaults in code and compose
    assert (
        "INFRA_PROBE_NEVER_GREEN_ESCALATION_FAILURES: "
        f"${{INFRA_PROBE_NEVER_GREEN_ESCALATION_FAILURES:-"
        f"{runner.DEFAULT_NEVER_GREEN_ESCALATION_FAILURES}}}" in compose
    )
    assert (
        "INFRA_PROBE_NEVER_GREEN_ESCALATION_SECONDS: "
        f"${{INFRA_PROBE_NEVER_GREEN_ESCALATION_SECONDS:-"
        f"{runner.DEFAULT_NEVER_GREEN_ESCALATION_SECONDS}}}" in compose
    )


def test_in_band_probe_compose_uses_internal_network_targets() -> None:
    """#183: in-band probes must not route through Cloudflare public domains.

    #541: the probe set is registry-rendered (ProbeFacet declarations on the
    owning Deployers), so the rule is checked on the rendered text."""
    from libs.probe_specs import render_probe_spec_text

    probe_block = render_probe_spec_text()

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
    # signoz is prod_only (single shared instance — see platform/11.signoz/deploy.py),
    # probed WITHOUT ${ENV_SUFFIX} from every env. A -staging suffix would target a
    # phantom host and fire a permanent false-positive alert.
    # ClickHouse is no longer probed by a read-only /ping (it stays green on an
    # unwritable data dir) — it has a write-path healthcheck + the roundtrip probes.
    assert "http://platform-signoz:8080/api/v1/health" in probe_block
    assert "http://platform-clickhouse:8123/ping" not in probe_block
    assert "platform-signoz${ENV_SUFFIX}" not in probe_block


def test_public_route_probes_derive_from_facets_and_registered_signals() -> None:
    """#543 (#209 REVERSED, operator-approved): public routes are probed from
    inside as the third layer on the same routes Cloudflare (30min) and the
    github-plane check (daily) already watch. The compose carries the env
    reference; the content derives from PublicRouteFacet declarations; and
    every rendered probe name MUST be a registered *-public-route signal —
    an unregistered public probe cannot ship."""
    import yaml

    from libs.probe_specs import parse_probe_names, render_public_route_spec_text

    compose = (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")
    assert "PUBLIC_ROUTE_PROBE_SPECS: ${PUBLIC_ROUTE_PROBE_SPECS:-}" in compose

    signals = yaml.safe_load(
        (ROOT / "docs/ssot/watchdog-signals.yaml").read_text(encoding="utf-8")
    )
    registered = {s.get("signal") for s in signals.get("signals", [])}

    prod = render_public_route_spec_text("production", "zitian.party")
    staging = render_public_route_spec_text("staging", "zitian.party")
    prod_names = parse_probe_names(prod)
    staging_names = parse_probe_names(staging)

    assert prod_names, "production must render public-route probes"
    for name in prod_names | staging_names:
        assert name in registered, f"unregistered public-route probe: {name}"
    # prod_only services (signoz) render for production only — the staging host
    # never exists (the false-positive class the old literal documented)
    assert "signoz-public-route" in prod_names
    assert "signoz-public-route" not in staging_names
    # non-production renders are warning severity — a broken staging route is
    # not a page-worthy production incident
    for line in staging.splitlines():
        assert "|warning|" in line
    # env_shared bootstrap hosts carry no env suffix in any environment
    assert "https://vault.zitian.party" in staging
    assert "https://cloud.zitian.party" in staging


def test_the_watchers_can_actually_speak_at_info(monkeypatch, capsys) -> None:
    """v1.1.78 on staging: the deploy-queue guard's new one-line-per-sweep summary
    appeared nowhere, because the process has no logging handler and `logging`'s handler
    of last resort emits WARNING and above. The old per-compose line was only ever
    visible for being a warning."""
    import logging

    runner = _load_probe_runner()
    watched = ("deploy-queue-guard", "container-breakdown-watch")
    root = logging.getLogger()
    saved = {
        "__root_handlers__": list(root.handlers),
        "__root_level__": root.level,
        **{name: logging.getLogger(name).level for name in watched},
    }
    try:
        # Build the precondition rather than inherit it: whatever the rest of the suite
        # has done to logging, this test starts from an unconfigured process.
        root.handlers = []
        root.setLevel(logging.WARNING)
        for name in watched:
            logging.getLogger(name).setLevel(logging.NOTSET)
        assert logging.getLogger(watched[0]).getEffectiveLevel() > logging.INFO

        runner._configure_logging()

        for name in watched:
            assert logging.getLogger(name).getEffectiveLevel() == logging.INFO, name
        # nothing else becomes chatty
        assert (
            logging.getLogger("some.other.library").getEffectiveLevel()
            == logging.WARNING
        )
        assert root.handlers, "a handler must exist or INFO records go nowhere"
    finally:
        root.handlers = saved.pop("__root_handlers__")
        root.setLevel(saved.pop("__root_level__"))
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def test_every_probe_cycle_leaves_one_line_of_positive_evidence(
    monkeypatch, tmp_path, capsys
) -> None:
    """#608: a passing probe is silent, so a log with no probe lines reads exactly like a
    runner probing nothing. On 2026-09-09 that is how this container's log read while 24
    infra-service and 7 public-route probes were passing every minute."""
    runner = _load_probe_runner()
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setenv("INFRA_PROBE_DRY_RUN", "1")
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [probes.run_probe(specs[0], http_get=lambda *_args: (200, "ok"))],
    )

    assert runner.run_once(state_path=tmp_path / "s.json") == 0
    out = capsys.readouterr().out
    assert "infra probes: infra-service 1/1 ok" in out
    assert "FAILURES" not in out

    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [
            probes.run_probe(specs[0], http_get=lambda *_args: (503, "sealed"))
        ],
    )
    assert runner.run_once(state_path=tmp_path / "s.json") == 1
    out = capsys.readouterr().out
    assert "infra probes: infra-service 0/1 ok" in out and "FAILURES" in out


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


def _result(spec, ok, observed="x"):
    return probes.ProbeResult(
        spec=spec, ok=ok, summary="s" if ok else "boom", observed=observed, elapsed_ms=1
    )


def test_never_green_command_probe_routes_to_misconfigured_warning(
    monkeypatch, tmp_path
) -> None:
    """A `command` probe (code that can be broken) that has NEVER succeeded is a
    misconfigured probe, not a real outage — route to InfraProbeMisconfigured at warning,
    never page critical (the signoz-roundtrip 500-storm class)."""
    runner = _load_probe_runner()
    posted: list[dict] = []

    monkeypatch.setenv("INFRA_PROBE_SPECS", "rt|command|do-roundtrip|ok|critical|5")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [_result(specs[0], ok=False, observed="RuntimeError")],
    )

    assert runner.run_once(state_path=tmp_path / "s.json", failure_threshold=1) == 1
    assert len(posted) == 1  # only the misconfigured stream; no critical page
    assert posted[0]["commonLabels"]["alertname"] == "InfraProbeMisconfigured"
    assert posted[0]["commonLabels"]["severity"] == "warning"
    assert posted[0]["alerts"][0]["labels"]["severity"] == "warning"


def test_once_green_command_probe_failure_is_a_real_regression(
    monkeypatch, tmp_path
) -> None:
    """Once a command probe has succeeded, a later failure is a real regression — it keeps
    its declared (critical) severity, not the misconfigured lane."""
    runner = _load_probe_runner()
    posted: list[dict] = []
    oks = [True, False]

    monkeypatch.setenv("INFRA_PROBE_SPECS", "rt|command|do-roundtrip|ok|critical|5")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    monkeypatch.setattr(
        runner, "run_probes", lambda specs: [_result(specs[0], ok=oks.pop(0))]
    )
    state_path = tmp_path / "s.json"

    assert runner.run_once(state_path=state_path, failure_threshold=1) == 0  # passes
    assert posted == []
    assert runner.run_once(state_path=state_path, failure_threshold=1) == 1  # regresses
    firing = [p for p in posted if p["status"] == "firing"]
    assert firing[0]["commonLabels"]["alertname"] == "InfraServiceProbeFailed"
    assert firing[0]["commonLabels"]["severity"] == "critical"


def test_never_green_http_probe_still_pages_critical(monkeypatch, tmp_path) -> None:
    """An http/tcp liveness probe is NOT eligible for the misconfigured lane: its failure
    is always a real target failure, so a service down at runner-boot still pages critical
    (must not be silently downgraded to warning)."""
    runner = _load_probe_runner()
    posted: list[dict] = []

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200|critical|5")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [
            probes.run_probe(specs[0], http_get=lambda *_a: (503, "sealed"))
        ],
    )

    assert runner.run_once(state_path=tmp_path / "s.json", failure_threshold=1) == 1
    assert posted[0]["commonLabels"]["alertname"] == "InfraServiceProbeFailed"
    assert posted[0]["commonLabels"]["severity"] == "critical"


def test_payload_severity_is_the_most_severe_failure() -> None:
    """One payload covers a group's failing probes. Its severity is the worst of them,
    not whichever rendered first (ops.observability §3: critical P0, error P1, warning P2)."""
    warning, error, critical, odd = (
        parse_probe_specs(f"{name}|http|http://x|200|{severity}|5")[0]
        for name, severity in (
            ("worker", "warning"),
            ("roundtrip", "error"),
            ("vault", "critical"),
            ("typo", "crtical"),
        )
    )
    down = [_result(warning, ok=False), _result(error, ok=False)]
    assert build_probe_alert_payload(down)["commonLabels"]["severity"] == "error"
    assert probes.group_severity(down) == "error"
    assert probes.group_severity([*down, _result(critical, ok=False)]) == "critical"
    # an unrecognised severity pages loudly, as a level the SSOT defines
    assert (
        probes.group_severity([_result(warning, ok=False), _result(odd, ok=False)])
        == "critical"
    )
    assert probes.group_severity([]) == "info"
    # the per-alert labels keep each probe's own severity, and an override still wins
    assert [
        a["labels"]["severity"] for a in build_probe_alert_payload(down)["alerts"]
    ] == [
        "warning",
        "error",
    ]
    overridden = build_probe_alert_payload(down, severity_override="warning")
    assert overridden["commonLabels"]["severity"] == "warning"


@pytest.mark.parametrize(
    ("failing", "paged"),
    [
        # 2026-09-15/16 (#726): NOSCRIPT, /healthcheck green, every /track lost
        ({"openpanel-roundtrip"}, ["openpanel-roundtrip"]),
        # API down: the round-trip is its cascade symptom, the API root pages
        ({"openpanel-api-http", "openpanel-roundtrip"}, ["openpanel-api-http"]),
        # worker down: events stop landing; the worker probe (P2) renders first
        (
            {"openpanel-worker-http", "openpanel-roundtrip"},
            ["openpanel-roundtrip", "openpanel-worker-http"],
        ),
    ],
    ids=["noscript", "api-down", "worker-down"],
)
def test_openpanel_ingest_loss_pages_p1_through_the_deployed_specs(
    monkeypatch, tmp_path, failing, paged
) -> None:
    """ops.observability §5: an OpenPanel ingest loss is P1 (`error`). Driven through the
    registry-rendered INFRA_PROBE_SPECS the alerting deploy ships. Red before: every
    shape paged `warning`; the worker shape stayed `warning` with the probes raised
    until the payload took the worst severity instead of the first."""
    from libs.probe_specs import (
        encode_specs_env_value,
        render_probe_spec_text,
        resolve_env_suffix,
    )

    runner = _load_probe_runner()
    posted: list[dict] = []
    monkeypatch.setenv(
        "INFRA_PROBE_SPECS",
        encode_specs_env_value(resolve_env_suffix(render_probe_spec_text(), "")),
    )
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    down: set[str] = set()
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [_result(s, ok=s.name not in down) for s in specs],
    )
    state_path = tmp_path / "s.json"

    assert runner.run_once(state_path=state_path, failure_threshold=1) == 0
    down.update(failing)
    assert runner.run_once(state_path=state_path, failure_threshold=1) == 1

    (page,) = _firing(posted, "InfraServiceProbeFailed")
    assert _firing(posted, "InfraProbeMisconfigured") == []
    assert sorted(a["labels"]["component"] for a in page["alerts"]) == paged
    assert page["commonLabels"]["severity"] == "error"


def _roundtrip_runner(monkeypatch, severity: str, outcome):
    """A runner probing one `command` round-trip whose result `outcome(spec)` decides,
    with a controllable clock. Returns (runner, posted payloads, clock, printed lines)."""
    runner = _load_probe_runner()
    posted: list[dict] = []
    printed: list[str] = []
    clock = {"now": 1_000_000.0}
    monkeypatch.setenv(
        "INFRA_PROBE_SPECS",
        "openpanel-roundtrip|command|python rt.py openpanel|roundtrip-ok|"
        f"{severity}|45||platform/openpanel",
    )
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    monkeypatch.setattr(runner, "run_probes", lambda specs: [outcome(specs[0])])
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    monkeypatch.setattr(
        "builtins.print", lambda *a, **_k: printed.append(" ".join(map(str, a)))
    )
    return runner, posted, clock, printed


def _backend_down(spec):
    """What the NOSCRIPT outage looked like: the round-trip ran and /track 500'd."""
    return run_probe(
        spec,
        command_runner=lambda *_a: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="roundtrip-failed backend=openpanel error=HTTP Error 500",
        ),
    )


def _config_missing(spec):
    return run_probe(
        spec,
        command_runner=lambda *_a: subprocess.CompletedProcess(
            args=[],
            returncode=probes.MISCONFIGURED_EXIT_CODE,
            stdout="",
            stderr="roundtrip-misconfigured backend=openpanel "
            "error=no OpenPanel client id for environment 'pr_7'",
        ),
    )


def _firing(posted: list[dict], alert_name: str) -> list[dict]:
    return [
        p
        for p in posted
        if p["status"] == "firing" and p["commonLabels"]["alertname"] == alert_name
    ]


def test_command_probe_exiting_ex_config_is_classified_misconfigured() -> None:
    spec = parse_probe_specs("rt|command|python rt.py|roundtrip-ok|warning|45")[0]
    config = _config_missing(spec)
    backend = _backend_down(spec)
    assert config.observed == "ProbeMisconfigured"
    assert "no OpenPanel client id" in config.summary
    assert probes.is_misconfigured(config) is True
    assert probes.is_misconfigured(backend) is False
    assert probes.is_misconfigured(run_probe(spec, command_runner=_ok_runner)) is False


def _ok_runner(*_args):
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout="roundtrip-ok", stderr=""
    )


@pytest.mark.parametrize("severity", ["warning", "critical"])
def test_never_green_round_trip_escalates_after_the_grace_period(
    monkeypatch, tmp_path, severity
) -> None:
    """#726: a runner recreated in the middle of an outage never saw the round-trip pass.
    For 15 min that stays in the misconfigured lane — worded as 'has not passed', not as
    a broken probe — and then fails in the normal stream at the DECLARED severity."""
    runner, posted, clock, printed = _roundtrip_runner(
        monkeypatch, severity, _backend_down
    )
    state_path = tmp_path / "s.json"

    def cycle(at: float) -> None:
        clock["now"] = 1_000_000.0 + at
        assert (
            runner.run_once(
                state_path=state_path,
                failure_threshold=1,
                recovery_threshold=1,
                escalation_failures=3,
                escalation_seconds=900,
            )
            == 1
        )

    for at in (0, 60, 120, 600):  # four failed runs, still inside the 900 s window
        cycle(at)
    assert _firing(posted, "InfraServiceProbeFailed") == []
    grace = _firing(posted, "InfraProbeMisconfigured")
    assert len(grace) == 1  # debounced per stream; the wording is stable
    assert grace[0]["commonLabels"]["severity"] == "warning"
    description = grace[0]["alerts"][0]["annotations"]["description"]
    assert description.startswith("has not passed since the probe runner started")
    assert "becomes InfraServiceProbeFailed after 3 failed runs over 15 min" in (
        description
    )
    assert "HTTP Error 500" in description

    cycle(900)  # the streak now spans the window: an outage, not a misconfiguration
    escalated = _firing(posted, "InfraServiceProbeFailed")
    assert len(escalated) == 1
    assert escalated[0]["commonLabels"]["severity"] == severity
    assert escalated[0]["alerts"][0]["labels"]["service_id"] == "platform/openpanel"
    assert "HTTP Error 500" in escalated[0]["alerts"][0]["annotations"]["description"]
    # the misconfigured lane lets go of it, and the escalation is logged once
    assert any(
        p["status"] == "resolved"
        and p["commonLabels"]["alertname"] == "InfraProbeMisconfigured"
        for p in posted
    )
    cycle(960)
    assert (
        sum(
            "probe-runner escalated probe=openpanel-roundtrip" in line
            for line in printed
        )
        == 1
    )
    assert len(_firing(posted, "InfraServiceProbeFailed")) == 1  # renotify window


def test_escalation_needs_both_the_run_count_and_the_window(
    monkeypatch, tmp_path
) -> None:
    """A failing round-trip re-runs on every 60 s loop, so three runs alone is three
    minutes; two runs an hour apart is not a streak of three either."""
    runner, posted, clock, _printed = _roundtrip_runner(
        monkeypatch, "warning", _backend_down
    )
    state_path = tmp_path / "s.json"

    def cycle(at: float) -> None:
        clock["now"] = 1_000_000.0 + at
        runner.run_once(
            state_path=state_path,
            failure_threshold=1,
            escalation_failures=3,
            escalation_seconds=900,
        )

    cycle(0)
    cycle(3600)
    assert _firing(posted, "InfraServiceProbeFailed") == []
    cycle(3660)
    assert len(_firing(posted, "InfraServiceProbeFailed")) == 1


def test_escalation_defaults_are_three_runs_over_fifteen_minutes(
    monkeypatch, tmp_path
) -> None:
    runner, posted, clock, _printed = _roundtrip_runner(
        monkeypatch, "warning", _backend_down
    )
    monkeypatch.delenv("INFRA_PROBE_NEVER_GREEN_ESCALATION_FAILURES", raising=False)
    monkeypatch.delenv("INFRA_PROBE_NEVER_GREEN_ESCALATION_SECONDS", raising=False)
    assert runner.DEFAULT_NEVER_GREEN_ESCALATION_FAILURES == 3
    assert runner.DEFAULT_NEVER_GREEN_ESCALATION_SECONDS == 900
    state_path = tmp_path / "s.json"
    for at in range(0, 900, 60):
        clock["now"] = 1_000_000.0 + at
        runner.run_once(state_path=state_path, failure_threshold=1)
    assert _firing(posted, "InfraServiceProbeFailed") == []
    clock["now"] = 1_000_900.0
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert len(_firing(posted, "InfraServiceProbeFailed")) == 1
    streak = json.loads(state_path.read_text())["never_green"]["openpanel-roundtrip"]
    assert streak["runs"] == 16 and streak["escalated"] is True


def test_a_round_trip_missing_its_own_configuration_never_escalates(
    monkeypatch, tmp_path
) -> None:
    """The client id / URL is absent: nothing about OpenPanel was tested, so it stays a
    misconfigured-probe warning however long it lasts — and even after the probe once
    passed (a deploy that dropped its config is still a config problem)."""
    outcomes = [_ok_runner] + [None] * 40

    def outcome(spec):
        runner_fn = outcomes.pop(0)
        return (
            run_probe(spec, command_runner=runner_fn)
            if runner_fn
            else _config_missing(spec)
        )

    runner, posted, clock, printed = _roundtrip_runner(monkeypatch, "critical", outcome)
    state_path = tmp_path / "s.json"
    for at in range(0, 7200, 180):  # two hours
        clock["now"] = 1_000_000.0 + at
        runner.run_once(
            state_path=state_path,
            failure_threshold=1,
            escalation_failures=3,
            escalation_seconds=900,
        )
    assert _firing(posted, "InfraServiceProbeFailed") == []
    misconfigured = _firing(posted, "InfraProbeMisconfigured")
    assert misconfigured and all(
        p["commonLabels"]["severity"] == "warning" for p in misconfigured
    )
    description = misconfigured[0]["alerts"][0]["annotations"]["description"]
    assert "no OpenPanel client id" in description
    assert "has not passed" not in description
    assert not any("escalated" in line for line in printed)
    assert json.loads(state_path.read_text())["never_green"] == {}


def test_a_config_failure_does_not_count_toward_a_later_backend_streak(
    monkeypatch, tmp_path
) -> None:
    """Config fixed after an hour, backend broken: the streak starts at the first run
    that actually tested the backend."""
    outcomes = [_config_missing] * 20 + [_backend_down] * 20
    runner, posted, clock, _printed = _roundtrip_runner(
        monkeypatch, "warning", lambda spec: outcomes.pop(0)(spec)
    )
    state_path = tmp_path / "s.json"
    for index, at in enumerate(range(0, 4800, 180)):
        clock["now"] = 1_000_000.0 + at
        runner.run_once(
            state_path=state_path,
            failure_threshold=1,
            escalation_failures=3,
            escalation_seconds=900,
        )
        if index == 21:  # two backend failures, 180 s apart
            assert _firing(posted, "InfraServiceProbeFailed") == []
    assert len(_firing(posted, "InfraServiceProbeFailed")) == 1


def test_an_unreadable_never_green_state_starts_over(monkeypatch, tmp_path) -> None:
    runner, posted, _clock, _printed = _roundtrip_runner(
        monkeypatch, "warning", _backend_down
    )
    state_path = tmp_path / "s.json"
    state_path.write_text(json.dumps({"groups": {}, "never_green": ["corrupt"]}))
    assert runner.run_once(state_path=state_path, failure_threshold=1) == 1
    streak = json.loads(state_path.read_text())["never_green"]["openpanel-roundtrip"]
    assert streak["runs"] == 1
    assert _firing(posted, "InfraProbeMisconfigured")


def test_never_green_streaks_of_removed_probes_are_dropped(
    monkeypatch, tmp_path
) -> None:
    runner, _posted, clock, _printed = _roundtrip_runner(
        monkeypatch, "warning", _backend_down
    )
    state_path = tmp_path / "s.json"
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert "openpanel-roundtrip" in json.loads(state_path.read_text())["never_green"]
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200|critical|5")
    monkeypatch.setattr(
        runner,
        "run_probes",
        lambda specs: [run_probe(specs[0], http_get=lambda *_a: (200, "ok"))],
    )
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert json.loads(state_path.read_text())["never_green"] == {}


def test_probe_spec_parses_depends_on_field() -> None:
    assert parse_probe_specs("x|http|http://x|200|critical|5|y")[0].depends_on == "y"
    assert (
        parse_probe_specs("z|http|http://z|200")[0].depends_on == ""
    )  # absent default


def _two_probe_runner(monkeypatch, ok: dict):
    """A runner with `root` and a `dep|...|root` probe, results driven by `ok` map."""
    runner = _load_probe_runner()
    posted: list[dict] = []
    monkeypatch.setenv(
        "INFRA_PROBE_SPECS",
        "root|http|http://root|200|critical|5\ndep|http|http://dep|200|critical|5|root",
    )
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    monkeypatch.setattr(
        runner, "run_probes", lambda specs: [_result(s, ok=ok[s.name]) for s in specs]
    )
    return runner, posted


def test_cascade_suppresses_dependent_when_root_also_fails(
    monkeypatch, tmp_path
) -> None:
    """A probe whose declared `depends_on` is ALSO failing is a cascade symptom — suppress
    it and page only the root (page the deepest failed node, not the cascade)."""
    runner, posted = _two_probe_runner(monkeypatch, {"root": False, "dep": False})
    assert runner.run_once(state_path=tmp_path / "s.json", failure_threshold=1) == 1
    firing = [p for p in posted if p["status"] == "firing"]
    services = {a["labels"]["service"] for a in firing[0]["alerts"]}
    assert services == {"root"}  # dep cascade-suppressed; only the root pages


def test_cascade_does_not_suppress_when_root_is_healthy(monkeypatch, tmp_path) -> None:
    """If the dependency is healthy, the dependent's failure is a real independent fault —
    it must still page (never silently swallowed by the cascade rule)."""
    runner, posted = _two_probe_runner(monkeypatch, {"root": True, "dep": False})
    assert runner.run_once(state_path=tmp_path / "s.json", failure_threshold=1) == 1
    firing = [p for p in posted if p["status"] == "firing"]
    services = {a["labels"]["service"] for a in firing[0]["alerts"]}
    assert services == {
        "dep"
    }  # not suppressed — root is fine, dep is a genuine failure


def test_cascade_cycle_is_not_suppressed_fail_closed(monkeypatch, tmp_path) -> None:
    """A depends_on CYCLE (A->B->A) where both fail has no root — neither is suppressed
    (fail closed → both alert). Otherwise a cycle of all-failing probes silently swallows
    every page."""
    runner = _load_probe_runner()
    posted: list[dict] = []
    monkeypatch.setenv(
        "INFRA_PROBE_SPECS",
        "A|http|http://a|200|critical|5|B\nB|http|http://b|200|critical|5|A",
    )
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    ok = {"A": False, "B": False}
    monkeypatch.setattr(
        runner, "run_probes", lambda specs: [_result(s, ok=ok[s.name]) for s in specs]
    )
    assert runner.run_once(state_path=tmp_path / "s.json", failure_threshold=1) == 1
    firing = [p for p in posted if p["status"] == "firing"]
    services = {a["labels"]["service"] for a in firing[0]["alerts"]}
    assert services == {"a", "b"}  # canonical labels; neither is suppressed


def test_cascade_chain_suppresses_middle_and_pages_deepest_root(
    monkeypatch, tmp_path
) -> None:
    """A->B->C (C has no dep), all failing: A and B are symptoms, only deepest root C pages."""
    runner = _load_probe_runner()
    posted: list[dict] = []
    monkeypatch.setenv(
        "INFRA_PROBE_SPECS",
        "A|http|http://a|200|critical|5|B\n"
        "B|http|http://b|200|critical|5|C\n"
        "C|http|http://c|200|critical|5",
    )
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    monkeypatch.setattr(
        runner, "post_alert_bridge_payload", lambda _u, p, **_k: posted.append(p)
    )
    ok = {"A": False, "B": False, "C": False}
    monkeypatch.setattr(
        runner, "run_probes", lambda specs: [_result(s, ok=ok[s.name]) for s in specs]
    )
    assert runner.run_once(state_path=tmp_path / "s.json", failure_threshold=1) == 1
    firing = [p for p in posted if p["status"] == "firing"]
    services = {a["labels"]["service"] for a in firing[0]["alerts"]}
    assert services == {"c"}  # canonical label; root C pages


def test_resolve_failing_root_returns_deepest_node_and_terminates() -> None:
    """The suppression log must name the true root (deepest failing node), not the immediate
    depends_on — A->B->C resolves to C — and must terminate on a cycle."""
    runner = _load_probe_runner()
    chain = {"A": "B", "B": "C"}  # A->B->C, C is the root
    failed = {"A", "B", "C"}
    assert runner._resolve_failing_root("A", chain, failed) == "C"
    assert runner._resolve_failing_root("B", chain, failed) == "C"
    assert runner._resolve_failing_root("C", chain, failed) == "C"
    cycle = {"A": "B", "B": "A"}  # must not loop forever
    assert runner._resolve_failing_root("A", cycle, {"A", "B"}) in {"A", "B"}


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
            "failing_public_routes": [],
            "last_delivery_ok_at": 0,
            "name": "platform-alerting-probes-staging",
            "ok": True,
            "schema": 2,
            "timestamp": 12345,
        },
    }


def test_probe_runner_liveness_ping_is_flagged_on_the_wire(monkeypatch) -> None:
    """The watchdog keeps the stored verdict for a flagged liveness ping; a verdict
    post (the exact payload above) carries no flag. 2026-09-15: the unflagged ping's
    ok=true alternated with a failing verdict and every alternation was a KV put."""
    runner = _load_probe_runner()
    sent: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"ok"

    def fake_urlopen(request, *, timeout):
        sent.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setenv(
        "INFRA_PROBE_HEARTBEAT_URL", "https://watchdog.example/heartbeat"
    )
    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    runner._post_heartbeat(
        ok=True, detail="probe loop iteration starting", liveness=True
    )
    runner._post_heartbeat(ok=False)

    assert sent[0]["liveness"] is True
    assert sent[0]["ok"] is True
    assert "liveness" not in sent[1]
    assert sent[1]["ok"] is False


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
    specs = parse_probe_specs("vault|http|http://vault|200\nhost-cpu|resource|cpu|80")

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


def test_probe_runner_posts_liveness_heartbeat_before_probes(monkeypatch) -> None:
    """#369: the looped runner emits a liveness heartbeat at the START of each iteration,
    BEFORE running probes — so a crash/hang during a probe cycle surfaces as heartbeat
    staleness within one interval, not only after a full (possibly slow) cycle."""
    runner = _load_probe_runner()
    events: list = []

    def fake_run_once(*, as_json, **_kwargs):
        events.append("run_once")
        return 0

    def fake_post_heartbeat(*, ok, detail="", liveness=False, state=None):
        events.append(("hb", detail, liveness))

    def fake_sleep(_seconds):
        raise SystemExit(0)

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    monkeypatch.setattr(runner, "_build_watchers", lambda: [])
    monkeypatch.setattr(runner, "_post_heartbeat", fake_post_heartbeat)
    monkeypatch.setattr(runner, "_touch_state", lambda _p: None)
    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--loop", "--json"])

    try:
        runner.main()
    except SystemExit:
        pass

    # flagged as liveness so the watchdog keeps the last probe verdict (KV budget)
    assert events[0] == ("hb", "probe loop iteration starting", True)
    assert "run_once" in events
    assert events.index(("hb", "probe loop iteration starting", True)) < events.index(
        "run_once"
    )


def test_probe_runner_skips_liveness_heartbeat_in_dry_run(monkeypatch) -> None:
    """#369 CR: dry-run must NOT emit the liveness heartbeat — no misleading liveness
    signal during local/debug runs (mirrors run_once's `if not dry_run` gate)."""
    runner = _load_probe_runner()
    beats: list = []

    def fake_run_once(*, as_json, **_kwargs):
        return 0

    def fake_sleep(_seconds):
        raise SystemExit(0)

    monkeypatch.setenv("INFRA_PROBE_DRY_RUN", "1")
    monkeypatch.setattr(runner, "run_once", fake_run_once)
    monkeypatch.setattr(runner, "_post_heartbeat", lambda **k: beats.append(k))
    monkeypatch.setattr(runner, "_touch_state", lambda _p: None)
    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--loop", "--json"])

    try:
        runner.main()
    except SystemExit:
        pass

    assert beats == []


def test_runner_dies_loudly_when_specs_env_set_but_empty(monkeypatch) -> None:
    """#541 fail-closed layer 3: compose's `${INFRA_PROBE_SPECS:-}` yields a
    SET-but-EMPTY env var when the renderer's output never arrives; os.getenv
    then returns "" (not the default), which previously meant a zero-probe
    runner with a green healthcheck — silent fleet blindness. The runner must
    exit non-zero so the container goes unhealthy and pages."""
    import pytest

    runner = _load_probe_runner()
    monkeypatch.setenv("INFRA_PROBE_SPECS", "")
    with pytest.raises(SystemExit, match="refusing to run blind"):
        runner._probe_groups()


def test_runner_unset_env_still_uses_default_specs(monkeypatch) -> None:
    """Unset (as opposed to set-but-empty) keeps the historical local-dev
    fallback to DEFAULT_PROBE_SPECS — only the transport-failure shape dies."""
    runner = _load_probe_runner()
    monkeypatch.delenv("INFRA_PROBE_SPECS", raising=False)
    groups = runner._probe_groups()
    assert groups[0].raw_specs == runner.DEFAULT_PROBE_SPECS


def test_truealpha_app_is_probed_inside_and_on_its_product_domain() -> None:
    """#608's open half: the two truealpha/app containers had no probe, and the app's
    public surface lives on its own domain (bare in production, prefixed in staging —
    truealpha#474), which the public-route renderer could not express."""
    import importlib.util

    from libs.probe_specs import render_probe_spec_text, render_public_route_spec_text

    internal = render_probe_spec_text()
    assert (
        "truealpha-web-http|http|http://truealpha-web${ENV_SUFFIX}:3000/|200|critical|5||truealpha/app"
        in internal
    )
    assert (
        "truealpha-llm-http|http|http://truealpha-llm${ENV_SUFFIX}:8000/health|200|critical|5||truealpha/app"
        in internal
    )

    prod = render_public_route_spec_text("production", "zitian.party")
    staging = render_public_route_spec_text("staging", "zitian.party")
    assert (
        "truealpha-web-public-route|http|https://truealpha.club/|200|critical|10||truealpha/app"
        in prod
    )
    assert (
        "truealpha-api-public-route|http|https://truealpha.club/api/health|200|critical"
        in prod
    )
    assert (
        "truealpha-web-public-route|http|https://truealpha-staging.truealpha.club/|200|warning"
        in staging
    )
    # finance_report keeps the shared-domain formula
    assert "https://report.zitian.party/api/health" in prod

    # The hosts are the ones the deploy ships as APP_HOST — one formula, two readers.
    spec = importlib.util.spec_from_file_location(
        "truealpha_app_deploy_for_probes", ROOT / "truealpha/truealpha/10.app/deploy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    overrides = module.AppDeployer.compose_env_overrides
    assert (
        overrides(env="production", domain="truealpha.club", env_suffix="")["APP_HOST"]
        == "truealpha.club"
    )
    assert (
        overrides(env="staging", domain="truealpha.club", env_suffix="-staging")[
            "APP_HOST"
        ]
        == "truealpha-staging.truealpha.club"
    )


# ---------------------------------------------------------------------------
# #903: identity fingerprint, no timer re-notify, daily digest, heartbeat v2


class _Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _HeartbeatResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b"ok"


def _scripted_runner(monkeypatch, *, env: str | None = "production", clock=None):
    """A runner whose probes return scripted results and whose bridge records posts.

    ``script["results"]`` maps probe name -> None (passing) or the observed reading of a
    failure; ``bridge["fail"]`` makes the next bridge POST raise the given exception.
    """
    runner = _load_probe_runner()
    posted: list[dict] = []
    beats: list[dict] = []
    script: dict = {"results": {}}
    bridge: dict = {"fail": None, "attempts": 0}

    for name in (
        "INFRA_PROBE_DRY_RUN",
        "INFRA_PROBE_MAINTENANCE_UNTIL",
        "INFRA_PROBE_RENOTIFY_SECONDS",
        "INFRA_ENVIRONMENT",
        "DEPLOY_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("ENV", "INFRA_PROBE_HEARTBEAT_ENV"):
        if env is None:  # a runner that was never told its environment
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, env)
    monkeypatch.setenv(
        "INFRA_PROBE_HEARTBEAT_URL", "https://watchdog.example/heartbeat"
    )

    def fake_post(_url, payload, **_kwargs):
        bridge["attempts"] += 1
        if bridge["fail"] is not None:
            raise bridge["fail"]
        posted.append(payload)
        return {}

    def fake_run_probes(specs):
        results = []
        for spec in specs:
            observed = script["results"].get(spec.name)
            if observed is None:
                results.append(_result(spec, True, observed="ok"))
            else:
                results.append(
                    probes.ProbeResult(
                        spec=spec,
                        ok=False,
                        summary=f"expected {spec.expected!r}, observed {observed!r}",
                        observed=observed,
                        elapsed_ms=1,
                    )
                )
        return results

    def fake_urlopen(request, *, timeout):
        beats.append(json.loads(request.data.decode("utf-8")))
        return _HeartbeatResponse()

    monkeypatch.setattr(runner, "post_alert_bridge_payload", fake_post)
    monkeypatch.setattr(runner, "run_probes", fake_run_probes)
    monkeypatch.setattr(runner, "urlopen", fake_urlopen)
    if clock is not None:
        monkeypatch.setattr(runner.time, "time", clock)
    return runner, posted, beats, script, bridge


_HOST_SPECS = "host-cpu|resource|cpu|80|warning\nhost-mem|resource|mem|80|warning"


def _firing_names(payload: dict) -> list[str]:
    return sorted(alert["labels"]["component"] for alert in payload["alerts"])


def test_a_resource_probe_whose_reading_changes_fires_after_the_threshold(
    monkeypatch, tmp_path
) -> None:
    """#903: a resource probe reads a new percentage every loop. Hashing the reading
    restarted the debounce each loop, so a host pinned above its ceiling never paged."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", _HOST_SPECS)
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    for reading in ("91.2", "93.5"):
        script["results"] = {"host-cpu": reading}
        runner.run_once(state_path=state_path, failure_threshold=3)
    assert posted == []  # below the threshold: two loops of the same failure
    script["results"] = {"host-cpu": "95.0"}
    runner.run_once(state_path=state_path, failure_threshold=3)

    assert [p["status"] for p in posted] == ["firing"]
    assert _firing_names(posted[0]) == ["host-cpu"]
    # the reading is still shown, it just is not identity
    assert posted[0]["alerts"][0]["annotations"]["observed"] == "95.0"


def test_an_active_stream_whose_readings_change_is_not_resent(
    monkeypatch, tmp_path
) -> None:
    """#903: once paged, an incident whose only change is its reading stays quiet —
    for hours, with no timer re-notify (the shipped default is 0)."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", _HOST_SPECS)
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    clock = _Clock(1_767_225_600.0)  # 2026-01-01T00:00Z
    runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch, clock=clock)
    state_path = tmp_path / "state.json"

    for step in range(20):  # 20 loops an hour apart: < 24h, so no digest either
        script["results"] = {"host-cpu": f"{81 + step * 0.7:.1f}"}
        runner.run_once(state_path=state_path, failure_threshold=3)
        clock.now += 3600

    assert [p["status"] for p in posted] == ["firing"]


def test_a_change_in_the_set_of_failing_probes_is_resent(monkeypatch, tmp_path) -> None:
    """#903: identity is WHICH probes fail. A second probe joining an active incident is
    news, and so is it leaving."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", _HOST_SPECS)
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    script["results"] = {"host-cpu": "90.0"}
    runner.run_once(state_path=state_path, failure_threshold=1)
    script["results"] = {"host-cpu": "91.0", "host-mem": "88.0"}
    runner.run_once(state_path=state_path, failure_threshold=1)
    script["results"] = {"host-cpu": "92.0"}
    runner.run_once(state_path=state_path, failure_threshold=1)

    assert [_firing_names(p) for p in posted] == [
        ["host-cpu"],
        ["host-cpu", "host-mem"],
        ["host-cpu"],
    ]


def test_a_change_in_failure_domain_is_resent(monkeypatch, tmp_path) -> None:
    """#903: the failure domain is identity too. A route blocked at the edge (1010) that
    turns into a real outage is a different incident and must reach the pager."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv(
        "PUBLIC_ROUTE_PROBE_SPECS", "vault-public-route|http|https://vault.example|200"
    )
    runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    script["results"] = {"vault-public-route": "403:error code: 1010"}
    runner.run_once(state_path=state_path, failure_threshold=1)
    runner.run_once(state_path=state_path, failure_threshold=1)
    script["results"] = {"vault-public-route": "521:origin down"}
    runner.run_once(state_path=state_path, failure_threshold=1)

    domains = [p["alerts"][0]["labels"]["failure_domain"] for p in posted]
    assert domains == ["probe-client-blocked", "service-or-route"]


def test_a_stream_failing_for_over_a_day_is_reported_once_per_utc_day(
    monkeypatch, tmp_path
) -> None:
    """#903: with no timer re-notify, a long outage resurfaces as a REPORT: the first
    loop it crosses a day, then at most once per UTC day — never as another page."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    start = 1_767_247_200.0  # 2026-01-01T06:00Z
    clock = _Clock(start)
    runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch, clock=clock)
    state_path = tmp_path / "state.json"
    script["results"] = {"vault": "503:sealed"}

    for _ in range(3 * 24 * 6 + 1):  # three days of 10-minute loops
        runner.run_once(state_path=state_path, failure_threshold=3)
        clock.now += 600

    pages = [p for p in posted if p["commonLabels"]["alertname"] != "InfraProbeChronic"]
    digests = [
        p for p in posted if p["commonLabels"]["alertname"] == "InfraProbeChronic"
    ]
    assert [p["status"] for p in pages] == ["firing"]
    assert all("delivery" not in p["commonLabels"] for p in pages)
    assert len(digests) == 3  # 01-02 (crossing a day), 01-03, 01-04
    for digest in digests:
        assert digest["commonLabels"]["delivery"] == "report"
        assert digest["commonLabels"]["severity"] == "warning"
        assert [a["labels"]["delivery"] for a in digest["alerts"]] == ["report"]
        assert (
            "infra-service failing for" in digest["alerts"][0]["annotations"]["summary"]
        )
        assert "vault" in digest["alerts"][0]["annotations"]["summary"]
    first = digests[0]["alerts"][0]["annotations"]["summary"]
    assert "failing for 1d 0h" in first  # not before the stream is a day old


def test_every_alert_from_a_non_production_runner_is_a_report(
    monkeypatch, tmp_path
) -> None:
    """#903: staging has no pager. Its runner labels every payload, firing and resolved,
    delivery=report; the production runner's stay pages."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)

    def lifecycle(env: str) -> list[dict]:
        runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch, env=env)
        state_path = tmp_path / f"{env}.json"
        script["results"] = {"vault": "503:sealed"}
        runner.run_once(state_path=state_path, failure_threshold=1)
        script["results"] = {}
        runner.run_once(state_path=state_path, recovery_threshold=1)
        return posted

    staging = lifecycle("staging")
    assert [p["status"] for p in staging] == ["firing", "resolved"]
    assert [p["commonLabels"].get("delivery") for p in staging] == ["report", "report"]
    assert all(a["labels"]["delivery"] == "report" for a in staging[0]["alerts"])

    production = lifecycle("production")
    assert [p["status"] for p in production] == ["firing", "resolved"]
    assert [p["commonLabels"].get("delivery") for p in production] == [None, None]


def test_a_failing_probe_is_not_a_heartbeat_failure(monkeypatch, tmp_path) -> None:
    """#903 heartbeat v2: `ok` is the LOOP's health. A probe failing used to flip it, and
    the Worker paged "probe loop failed" 401 times in 30 days. Failing public routes
    travel in their own field instead."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv(
        "PUBLIC_ROUTE_PROBE_SPECS",
        "b-public-route|http|https://b.example|200\na-public-route|http|https://a.example|200",
    )
    clock = _Clock(1_767_225_600.0)
    runner, posted, beats, script, _bridge = _scripted_runner(monkeypatch, clock=clock)
    script["results"] = {
        "vault": "503:sealed",
        "b-public-route": "521:down",
        "a-public-route": "522:down",
    }

    assert runner.run_once(state_path=tmp_path / "state.json", failure_threshold=1) == 1

    assert len(posted) == 2  # both streams paged
    assert beats[-1]["ok"] is True
    assert beats[-1]["detail"] == "probe loop completed"
    assert beats[-1]["schema"] == 2
    assert beats[-1]["failing_public_routes"] == ["a-public-route", "b-public-route"]
    assert beats[-1]["last_delivery_ok_at"] == 1_767_225_600


def test_a_failed_bridge_delivery_fails_the_heartbeat_until_a_retry_lands(
    monkeypatch, tmp_path
) -> None:
    """#903: the heartbeat is false while the latest bridge delivery failed, naming the
    HTTP status; the undelivered page is retried next loop, and a landed retry both
    clears the heartbeat and stops the resending."""
    from urllib.error import HTTPError

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    clock = _Clock(1_767_225_600.0)
    runner, posted, beats, script, bridge = _scripted_runner(monkeypatch, clock=clock)
    state_path = tmp_path / "state.json"
    script["results"] = {"vault": "503:sealed"}

    bridge["fail"] = HTTPError("http://bridge", 502, "Bad Gateway", None, None)
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert posted == []
    assert beats[-1]["ok"] is False
    assert beats[-1]["detail"] == "bridge delivery failed: HTTP 502 (infra-service)"
    assert beats[-1]["last_delivery_ok_at"] == 0

    bridge["fail"] = None
    clock.now += 60
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert [p["status"] for p in posted] == ["firing"]
    assert beats[-1]["ok"] is True
    assert beats[-1]["last_delivery_ok_at"] == 1_767_225_660

    clock.now += 60
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert bridge["attempts"] == 2  # delivered once: not sent a third time


def test_an_undelivered_resolve_is_sent_again(monkeypatch, tmp_path) -> None:
    """#903: a resolve is recorded before it is sent. If the bridge refuses it, the
    stream is rolled back, so the next loop resolves it again instead of leaving the
    page open forever."""
    from urllib.error import URLError

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, posted, beats, script, bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    script["results"] = {"vault": "503:sealed"}
    runner.run_once(state_path=state_path, failure_threshold=1, recovery_threshold=1)
    script["results"] = {}
    bridge["fail"] = URLError("connection refused")
    runner.run_once(state_path=state_path, failure_threshold=1, recovery_threshold=1)
    assert beats[-1]["ok"] is False
    assert beats[-1]["detail"].startswith("bridge delivery failed: URLError")

    bridge["fail"] = None
    runner.run_once(state_path=state_path, failure_threshold=1, recovery_threshold=1)
    assert [p["status"] for p in posted] == ["firing", "resolved"]
    assert beats[-1]["ok"] is True


def test_a_group_that_raises_is_named_and_the_other_group_still_alerts(
    monkeypatch, tmp_path
) -> None:
    """#903: one group raising no longer aborts the loop — the heartbeat names it, the
    next group still pages, and the state still saves."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv(
        "PUBLIC_ROUTE_PROBE_SPECS", "vault-public-route|http|https://vault.example|200"
    )
    runner, posted, beats, script, _bridge = _scripted_runner(monkeypatch)
    scripted = runner.run_probes

    def run_probes(specs):
        if specs[0].name == "vault":
            raise TimeoutError("probe pool wedged")
        return scripted(specs)

    monkeypatch.setattr(runner, "run_probes", run_probes)
    script["results"] = {"vault-public-route": "521:down"}
    state_path = tmp_path / "state.json"

    assert runner.run_once(state_path=state_path, failure_threshold=1) == 1

    assert [p["commonLabels"]["alertname"] for p in posted] == [
        "InfraPublicRouteProbeFailed"
    ]
    assert beats[-1]["ok"] is False
    assert beats[-1]["detail"].startswith("group infra-service raised TimeoutError")
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["groups"]["public-route"]["active"] is True


def test_a_state_save_failure_fails_the_heartbeat(monkeypatch, tmp_path) -> None:
    """#903: a runner that cannot persist its state loses its dedup memory on the next
    restart. That is a loop failure, and the heartbeat says so."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, _posted, beats, _script, _bridge = _scripted_runner(monkeypatch)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    runner.run_once(state_path=blocker / "state.json")

    assert beats[-1]["ok"] is False
    assert beats[-1]["detail"].startswith("state save failed: ")


def test_last_delivery_ok_at_survives_a_runner_restart(monkeypatch, tmp_path) -> None:
    """#903: the last bridge 2xx is persisted, so a restarted runner keeps reporting it
    instead of claiming it never delivered; 0 means no delivery yet."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    state_path = tmp_path / "state.json"

    clock = _Clock(1_767_225_600.0)
    runner, _posted, beats, script, _bridge = _scripted_runner(monkeypatch, clock=clock)
    runner.run_once(state_path=state_path)
    assert beats[-1]["last_delivery_ok_at"] == 0  # all green: nothing delivered yet
    script["results"] = {"vault": "503:sealed"}
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert beats[-1]["last_delivery_ok_at"] == 1_767_225_600

    clock.now += 7200
    restarted, posted, beats, script, _bridge = _scripted_runner(
        monkeypatch, clock=clock
    )
    script["results"] = {"vault": "503:sealed"}
    restarted.run_once(state_path=state_path, failure_threshold=1)
    assert posted == []  # same incident: nothing new to send
    assert beats[-1]["last_delivery_ok_at"] == 1_767_225_600


def test_the_liveness_ping_carries_the_v2_fields_of_the_last_loop(
    monkeypatch, tmp_path
) -> None:
    """#903: every heartbeat POST is schema 2; the liveness ping before the probes
    repeats the last completed loop's delivery time and failing routes."""
    runner, _posted, beats, _script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "groups": {},
                "last_delivery_ok_at": 1_767_225_000,
                "failing_public_routes": ["vault-public-route"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("INFRA_PROBE_STATE_FILE", str(state_path))
    monkeypatch.setattr(runner, "run_once", lambda **_kwargs: 0)
    monkeypatch.setattr(runner, "_build_watchers", lambda: [])
    monkeypatch.setattr(runner, "_touch_state", lambda _p: None)

    def stop(_seconds):
        raise SystemExit(0)

    monkeypatch.setattr(runner.time, "sleep", stop)
    monkeypatch.setattr("sys.argv", ["infra_probe_runner.py", "--loop"])
    with pytest.raises(SystemExit):
        runner.main()

    assert beats[0]["liveness"] is True
    assert beats[0]["schema"] == 2
    assert beats[0]["last_delivery_ok_at"] == 1_767_225_000
    assert beats[0]["failing_public_routes"] == ["vault-public-route"]


# --- #903 review: allowlisted environments, per-stream delivery failures, per-probe debounce


def test_only_a_recognised_non_production_runner_reports(monkeypatch, tmp_path) -> None:
    """#903 review: `!= "production"` turned `prod` or a typo into silence. Only an
    environment on the report-only allowlist reports; anything else pages."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)

    def delivery_of_first_page(env: str | None) -> str | None:
        runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch, env=env)
        script["results"] = {"vault": "503:sealed"}
        runner.run_once(state_path=tmp_path / f"{env!r}.json", failure_threshold=1)
        assert len(posted) == 1, env
        return posted[0]["commonLabels"].get("delivery")

    for pages in ("prod", "PRODUCTION ", None, "garbage"):
        assert delivery_of_first_page(pages) is None, pages
    for reports in ("staging", "STG", "pr-5"):
        assert delivery_of_first_page(reports) == "report", reports


def test_host_probes_stay_on_unless_the_runner_is_recognisably_non_production(
    monkeypatch,
) -> None:
    """#903 review: the host-probe gate compared exactly too, so a production runner
    told `prod` stopped watching the host. Only a recognised non-production runner
    drops the shared-host probes."""
    runner = _load_probe_runner()
    specs = parse_probe_specs("vault|http|http://vault|200\nhost-cpu|resource|cpu|80")
    for name in ("ENV", "DEPLOY_ENV"):
        monkeypatch.delenv(name, raising=False)

    for env in ("prod", "PRODUCTION ", "garbage"):
        monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_ENV", env)
        kinds = {spec.kind for spec in runner._host_specs_for_env(specs)}
        assert kinds == {"http", "resource"}, env
    monkeypatch.delenv("INFRA_PROBE_HEARTBEAT_ENV")
    assert {s.kind for s in runner._host_specs_for_env(specs)} == {"http", "resource"}
    monkeypatch.setenv("INFRA_PROBE_HEARTBEAT_ENV", "stg")
    assert {s.kind for s in runner._host_specs_for_env(specs)} == {"http"}


def test_a_failed_delivery_stops_failing_the_heartbeat_once_nothing_is_pending(
    monkeypatch, tmp_path
) -> None:
    """#903 review: the error used to clear only on a later success. A stream whose
    page never went out and that then recovered — never active, nothing left to send —
    held the heartbeat red for as long as the bridge stayed quiet."""
    from urllib.error import HTTPError

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, posted, beats, script, bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"
    bridge["fail"] = HTTPError("http://bridge", 503, "Unavailable", None, None)

    script["results"] = {"vault": "503:sealed"}
    runner.run_once(state_path=state_path, failure_threshold=1)
    assert beats[-1]["ok"] is False
    script["results"] = {}  # recovered before its page ever landed
    runner.run_once(state_path=state_path, failure_threshold=1)

    assert bridge["attempts"] == 1  # nothing was retried: there was nothing to send
    assert posted == []
    assert beats[-1]["ok"] is True
    assert beats[-1]["detail"] == "probe loop completed"


def test_a_delivery_that_keeps_failing_keeps_the_heartbeat_red(
    monkeypatch, tmp_path
) -> None:
    """#903 review: the clearing must not swallow a page that never went out. A
    bridge that refuses every report (say a wrong FEISHU_REPORT_CHAT_ID) is retried
    every loop and holds the heartbeat red every loop."""
    from urllib.error import HTTPError

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, posted, beats, script, bridge = _scripted_runner(monkeypatch, env="staging")
    state_path = tmp_path / "state.json"
    bridge["fail"] = HTTPError("http://bridge", 502, "Bad Gateway", None, None)
    script["results"] = {"vault": "503:sealed"}

    for _ in range(5):
        runner.run_once(state_path=state_path, failure_threshold=1)

    assert bridge["attempts"] == 5
    assert [beat["ok"] for beat in beats] == [False] * 5
    assert posted == []


def test_a_hard_down_probe_pages_while_a_sibling_flaps(monkeypatch, tmp_path) -> None:
    """#903 review: the debounce counted the WHOLE failing set, so a sibling failing
    every other loop reset it and a probe that was hard down never paged. Each probe
    now counts its own streak; the page names only what crossed the threshold."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", _HOST_SPECS)
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    for loop in range(3):
        script["results"] = {"host-cpu": "97.0"}
        if loop % 2 == 0:
            script["results"]["host-mem"] = "85.0"  # flapping sibling
        runner.run_once(state_path=state_path, failure_threshold=3)

    assert [p["status"] for p in posted] == ["firing"]
    assert _firing_names(posted[0]) == ["host-cpu"]


def test_a_sibling_flapping_below_the_threshold_does_not_re_page(
    monkeypatch, tmp_path
) -> None:
    """#903 review: once a probe has paged, a sibling that fails every other loop —
    never three in a row — is not news: no re-page each time it joins or leaves."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", _HOST_SPECS)
    monkeypatch.delenv("PUBLIC_ROUTE_PROBE_SPECS", raising=False)
    runner, posted, _beats, script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    script["results"] = {"host-cpu": "97.0"}
    for _ in range(3):
        runner.run_once(state_path=state_path, failure_threshold=3)
    assert [_firing_names(p) for p in posted] == [["host-cpu"]]

    for loop in range(10):
        script["results"] = {"host-cpu": "97.0"}
        if loop % 2 == 0:
            script["results"]["host-mem"] = "85.0"
        runner.run_once(state_path=state_path, failure_threshold=3)

    assert [_firing_names(p) for p in posted] == [["host-cpu"]]


# --- #903 x #911: failing_public_routes lists only routes whose page was delivered

_TWO_ROUTES = "a-public-route|http|https://a.example|200\nb-public-route|http|https://b.example|200"


def test_failing_public_routes_stays_empty_while_nothing_has_paged(
    monkeypatch, tmp_path
) -> None:
    """#911 review (HIGH): the Worker stands down its entrypoint page for every route
    listed. Two routes failing on alternate loops never reach the threshold, so the
    VPS never paged — listing them silenced both sides."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv("PUBLIC_ROUTE_PROBE_SPECS", _TWO_ROUTES)
    runner, posted, beats, script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    for loop in range(6):
        failing = "a-public-route" if loop % 2 == 0 else "b-public-route"
        script["results"] = {failing: "521:down"}
        runner.run_once(state_path=state_path, failure_threshold=3)

    assert posted == []
    assert [beat["failing_public_routes"] for beat in beats] == [[]] * 6


def test_failing_public_routes_stays_empty_while_the_page_is_undelivered(
    monkeypatch, tmp_path
) -> None:
    """#911 review: a route whose page the bridge refused has told nobody yet."""
    from urllib.error import HTTPError

    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv("PUBLIC_ROUTE_PROBE_SPECS", _TWO_ROUTES)
    runner, posted, beats, script, bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"
    bridge["fail"] = HTTPError("http://bridge", 502, "Bad Gateway", None, None)
    script["results"] = {"a-public-route": "521:down"}

    for _ in range(2):
        runner.run_once(state_path=state_path, failure_threshold=1)

    assert bridge["attempts"] == 2 and posted == []
    assert [beat["failing_public_routes"] for beat in beats] == [[], []]


def test_failing_public_routes_stays_empty_during_maintenance(
    monkeypatch, tmp_path
) -> None:
    """#911 review: maintenance skips the sends, so the Worker must not stand down on
    the VPS's behalf — even for a route paged before the window opened."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv("PUBLIC_ROUTE_PROBE_SPECS", _TWO_ROUTES)
    clock = _Clock(1_767_225_600.0)
    runner, _posted, beats, script, _bridge = _scripted_runner(monkeypatch, clock=clock)
    state_path = tmp_path / "state.json"
    script["results"] = {"a-public-route": "521:down"}

    runner.run_once(state_path=state_path, failure_threshold=1)
    assert beats[-1]["failing_public_routes"] == ["a-public-route"]
    monkeypatch.setenv("INFRA_PROBE_MAINTENANCE_UNTIL", str(clock.now + 3600))
    clock.now += 60
    runner.run_once(state_path=state_path, failure_threshold=1)

    assert beats[-1]["failing_public_routes"] == []


def test_failing_public_routes_lists_a_route_once_its_page_is_delivered(
    monkeypatch, tmp_path
) -> None:
    """#911 review: a route is listed from the loop its page lands until it stops
    failing — and a sibling still below the threshold is not listed with it."""
    monkeypatch.setenv("INFRA_PROBE_SPECS", "vault|http|http://vault|200")
    monkeypatch.setenv("PUBLIC_ROUTE_PROBE_SPECS", _TWO_ROUTES)
    runner, posted, beats, script, _bridge = _scripted_runner(monkeypatch)
    state_path = tmp_path / "state.json"

    script["results"] = {"a-public-route": "521:down"}
    runner.run_once(state_path=state_path, failure_threshold=3, recovery_threshold=2)
    runner.run_once(state_path=state_path, failure_threshold=3, recovery_threshold=2)
    assert beats[-1]["failing_public_routes"] == []  # below the threshold
    script["results"] = {"a-public-route": "521:down", "b-public-route": "522:down"}
    runner.run_once(state_path=state_path, failure_threshold=3, recovery_threshold=2)
    assert [p["status"] for p in posted] == ["firing"]
    assert beats[-1]["failing_public_routes"] == ["a-public-route"]

    script["results"] = {}  # passing again; the page is still open (recovery 1/2)
    runner.run_once(state_path=state_path, failure_threshold=3, recovery_threshold=2)
    assert beats[-1]["failing_public_routes"] == []
