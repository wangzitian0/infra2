"""Tests for SigNoz/OpenPanel synthetic round-trip probes."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from tools import observability_roundtrip_probe as probe
from common.core import registry
from common.core.service_identity import ServiceIdentity
from common.observability.openpanel import OPENPANEL_CLIENTS
from common.tests.compose_env import container_env

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_CONTRACT_DOC = ROOT / "docs/ssot/core.environments.md"
IDENTITY_CONTRACT_ANCHOR = '<a id="telemetry-identity"></a>'
ALERTING_COMPOSE = ROOT / "platform/12.alerting/compose.yaml"
#: The compatibility key that the contract dual-writes during the migration.
LEGACY_ENVIRONMENT_KEY = "deployment.environment"
SHA40 = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"


def test_signoz_roundtrip_posts_otlp_log_and_queries_nonce(monkeypatch) -> None:
    posted: list[dict] = []
    queries: list[dict] = []

    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "staging")
    monkeypatch.setenv("SIGNOZ_OTLP_LOGS_URL", "http://collector/v1/logs")
    monkeypatch.setenv("SIGNOZ_CLICKHOUSE_URL", "http://clickhouse:8123")
    monkeypatch.setattr(
        probe,
        "_post_json",
        lambda url, payload, **_kwargs: posted.append({"url": url, "payload": payload}),
    )

    def fake_wait(url, query):
        queries.append({"url": url, "query": query})
        return 1

    monkeypatch.setattr(probe, "_wait_for_count", fake_wait)

    result = probe.run_signoz_roundtrip("nonce-1")

    assert result.backend == "signoz"
    assert result.count == 1
    assert posted[0]["url"] == "http://collector/v1/logs"
    resource_attrs = posted[0]["payload"]["resourceLogs"][0]["resource"]["attributes"]
    assert {
        "key": "service.name",
        "value": {"stringValue": "infra-observability-canary"},
    } in resource_attrs
    assert {
        "key": "deployment.environment.name",
        "value": {"stringValue": "staging"},
    } in resource_attrs
    # The contract dual-writes the legacy key during the migration (#1452).
    assert {
        "key": LEGACY_ENVIRONMENT_KEY,
        "value": {"stringValue": "staging"},
    } in resource_attrs
    log_record = posted[0]["payload"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][
        0
    ]
    assert "nonce-1" in log_record["body"]["stringValue"]
    assert queries[0]["url"] == "http://clickhouse:8123"
    assert "signoz_logs.distributed_logs_v2" in queries[0]["query"]
    assert "nonce-1" in queries[0]["query"]
    # distributed_logs_v2.timestamp is UInt64 nanoseconds. The time bound must be
    # converted to nanoseconds; comparing the column against a bare DateTime64
    # bound overflows in ClickHouse (Code 407) and 500s the probe on every cycle.
    assert (
        "timestamp >= toUnixTimestamp64Nano(now64(3) - INTERVAL 10 MINUTE)"
        in queries[0]["query"]
    )
    assert "AND timestamp >= now64(3)" not in queries[0]["query"]


def test_openpanel_roundtrip_tracks_event_and_queries_nonce(monkeypatch) -> None:
    posted: list[dict] = []
    queries: list[dict] = []

    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "staging")
    monkeypatch.setenv("OPENPANEL_ROUNDTRIP_API_URL", "http://openpanel-api:3000")
    monkeypatch.setenv("OPENPANEL_CLICKHOUSE_URL", "http://openpanel-ch:8123")
    monkeypatch.setattr(
        probe,
        "_post_json",
        lambda url, payload, **kwargs: posted.append(
            {"url": url, "payload": payload, "headers": kwargs.get("headers")}
        ),
    )

    def fake_wait(url, query):
        queries.append({"url": url, "query": query})
        return 2

    monkeypatch.setattr(probe, "_wait_for_count", fake_wait)

    result = probe.run_openpanel_roundtrip("nonce-2")

    assert result.backend == "openpanel"
    assert result.count == 2
    assert posted[0]["url"] == "http://openpanel-api:3000/track"
    assert posted[0]["headers"] == {"openpanel-client-id": OPENPANEL_CLIENTS["staging"]}
    body = posted[0]["payload"]
    assert body["type"] == "track"
    assert body["payload"]["name"] == "infra_observability_roundtrip_canary"
    assert body["payload"]["properties"]["deployment_environment"] == "staging"
    assert body["payload"]["properties"]["infra_nonce"] == "nonce-2"
    assert queries[0]["url"] == "http://openpanel-ch:8123"
    assert "openpanel.events" in queries[0]["query"]
    assert "properties['infra_nonce'] = 'nonce-2'" in queries[0]["query"]


def test_openpanel_roundtrip_includes_optional_client_secret(monkeypatch) -> None:
    posted: list[dict] = []

    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "production")
    monkeypatch.setenv("OPENPANEL_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        probe,
        "_post_json",
        lambda _url, _payload, **kwargs: posted.append(kwargs.get("headers", {})),
    )
    monkeypatch.setattr(probe, "_wait_for_count", lambda *_args: 1)

    probe.run_openpanel_roundtrip("nonce-3")

    assert posted[0]["openpanel-client-secret"] == "secret"


def test_main_suppresses_per_backend_until_interval(
    monkeypatch, tmp_path, capsys
) -> None:
    state_path = tmp_path / "roundtrip-state.json"
    state_path.write_text(
        json.dumps({"signoz": {"last_success_at": 1000.0, "last_nonce": "old"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["observability_roundtrip_probe.py", "signoz"])
    monkeypatch.setattr(probe.time, "time", lambda: 1100.0)
    monkeypatch.setenv("OBS_ROUNDTRIP_STATE_FILE", str(state_path))
    monkeypatch.setenv("OBS_ROUNDTRIP_INTERVAL_SECONDS", "3600")
    monkeypatch.setattr(
        probe,
        "run_signoz_roundtrip",
        lambda _nonce: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    assert probe.main() == 0

    out = capsys.readouterr().out
    assert "roundtrip-ok" in out
    assert "backend=signoz" in out
    assert "mode=suppressed" in out


def test_clickhouse_string_literal_escapes_quotes_and_backslashes() -> None:
    assert probe._ch_string("a'b\\c") == "'a\\'b\\\\c'"


def test_post_json_accepts_successful_non_json_body(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(probe.urllib.request, "urlopen", fake_urlopen)

    result = probe._post_json("http://internal/track", {"type": "track"})

    assert result == {"raw": "ok"}
    assert captured["url"] == "http://internal/track"
    assert json.loads(captured["body"]) == {"type": "track"}


def _run_main(monkeypatch, tmp_path, backend: str) -> int:
    monkeypatch.setattr(sys, "argv", ["observability_roundtrip_probe.py", backend])
    monkeypatch.setenv("OBS_ROUNDTRIP_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("ALERTING_ENV_FILE", str(tmp_path / "absent.env"))
    return probe.main()


def test_main_exits_ex_config_when_the_openpanel_client_id_is_missing(
    monkeypatch, tmp_path, capsys
) -> None:
    """#726: a probe that cannot run says so with EX_CONFIG, which the probe runner
    keeps in its misconfigured lane instead of escalating it as an outage."""
    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "pr_7")  # no OpenPanel project
    monkeypatch.delenv("OPENPANEL_CLIENT_ID", raising=False)
    monkeypatch.setattr(
        probe,
        "_post_json",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not post")),
    )

    assert (
        _run_main(monkeypatch, tmp_path, "openpanel") == probe.MISCONFIGURED_EXIT_CODE
    )
    assert probe.MISCONFIGURED_EXIT_CODE == os.EX_CONFIG
    err = capsys.readouterr().err
    assert err.startswith("roundtrip-misconfigured backend=openpanel")
    assert "no OpenPanel client id for environment 'pr_7'" in err
    assert not (tmp_path / "state.json").exists()


def test_main_exits_ex_config_on_an_invalid_backend_url(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "staging")
    monkeypatch.setenv("SIGNOZ_OTLP_LOGS_URL", "platform-signoz-otel-collector/v1/logs")

    assert _run_main(monkeypatch, tmp_path, "signoz") == os.EX_CONFIG
    assert "invalid URL" in capsys.readouterr().err


def test_main_reports_a_backend_failure_as_an_ordinary_failure(
    monkeypatch, tmp_path, capsys
) -> None:
    """The NOSCRIPT outage: configuration fine, /track answers 500. Exit 1 — a
    statement about OpenPanel, which the runner escalates."""
    import urllib.error

    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "production")

    def track_500(url, *_a, **_k):
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(probe, "_post_json", track_500)

    assert _run_main(monkeypatch, tmp_path, "openpanel") == 1
    err = capsys.readouterr().err
    assert err.startswith("roundtrip-failed backend=openpanel")
    assert "HTTP Error 500" in err


# ---- uniform identity of the canary log record (#1452) -------------------------------


def _contract_keys() -> set[str]:
    """The canonical resource keys of the telemetry identity table, parsed from the SSOT."""
    lines = IDENTITY_CONTRACT_DOC.read_text(encoding="utf-8").splitlines()
    assert lines.count(IDENTITY_CONTRACT_ANCHOR) == 1, (
        "the identity anchor must exist once"
    )
    rows: list[str] = []
    for line in lines[lines.index(IDENTITY_CONTRACT_ANCHOR) + 1 :]:
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    keys: set[str] = set()
    for row in rows[2:]:  # skip the header and the separator
        canonical_cell = row.strip().strip("|").split("|")[1]
        keys.update(
            token.split("=", 1)[0] for token in re.findall(r"`([^`]+)`", canonical_cell)
        )
    # A parse that finds nothing would make every assertion below pass for nothing.
    assert len(keys) >= 9, f"the identity table parse found only {sorted(keys)}"
    return keys


def _signoz_resource(monkeypatch, *, env: str = "staging", iac_ref: str | None = SHA40):
    """The resource attributes of the log that the signoz round-trip posts, as a dict."""
    posted: list[dict] = []
    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", env)
    if iac_ref is None:
        monkeypatch.delenv("INFRA_IAC_REF", raising=False)
    else:
        monkeypatch.setenv("INFRA_IAC_REF", iac_ref)
    monkeypatch.setattr(
        probe, "_post_json", lambda _url, payload, **_kw: posted.append(payload)
    )
    monkeypatch.setattr(probe, "_wait_for_count", lambda *_args: 1)

    probe.run_signoz_roundtrip("nonce-1")

    attributes = posted[0]["resourceLogs"][0]["resource"]["attributes"]
    keys = [attribute["key"] for attribute in attributes]
    assert len(keys) == len(set(keys)), f"duplicate resource keys: {keys}"
    return {a["key"]: a["value"]["stringValue"] for a in attributes}


def test_signoz_log_resource_carries_every_key_of_the_identity_contract(
    monkeypatch,
) -> None:
    resource = _signoz_resource(monkeypatch)

    expected_keys = _contract_keys() | {LEGACY_ENVIRONMENT_KEY}
    assert sorted(expected_keys - resource.keys()) == []
    assert [key for key in expected_keys if not resource[key]] == []


def test_signoz_log_resource_equals_the_identity_builder_output(monkeypatch) -> None:
    resource = _signoz_resource(monkeypatch, env="staging", iac_ref=SHA40)

    identity = ServiceIdentity.build(
        "platform/signoz",
        "staging",
        component="signoz-roundtrip",
        service_name="infra-observability-canary",
        version=SHA40,
        iac_ref=SHA40,
    )
    built = dict(
        pair.split("=", 1) for pair in identity.otel_resource_attributes().split(",")
    )
    assert resource == {**built, "infra.canary.nonce": "nonce-1"}
    # The values that the SigNoz query and the alert labels depend on.
    assert resource["service.name"] == "infra-observability-canary"
    assert resource["infra.service.id"] == "platform/signoz"
    assert resource["infra.component"] == "signoz-roundtrip"
    assert resource["service.version"] == SHA40
    assert resource["infra.iac.ref"] == SHA40
    assert resource[LEGACY_ENVIRONMENT_KEY] == resource["deployment.environment.name"]


def test_signoz_log_resource_marks_the_version_unknown_when_no_release_ref_exists(
    monkeypatch,
) -> None:
    resource = _signoz_resource(monkeypatch, iac_ref=None)

    assert resource["service.version"] == "unknown"
    # The builder drops an empty ref; a fake ref would lie about the release.
    assert "infra.iac.ref" not in resource
    assert resource["infra.service.id"] == "platform/signoz"
    assert resource[LEGACY_ENVIRONMENT_KEY] == "staging"


def test_signoz_roundtrip_with_a_malformed_release_ref_is_misconfigured(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("OBS_ROUNDTRIP_ENV", "staging")
    monkeypatch.setenv("INFRA_IAC_REF", "not-a-sha")
    monkeypatch.setattr(
        probe,
        "_post_json",
        lambda *_a, **_k: pytest.fail("the probe must stop before it posts"),
    )

    assert _run_main(monkeypatch, tmp_path, "signoz") == probe.MISCONFIGURED_EXIT_CODE
    err = capsys.readouterr().err
    assert err.startswith("roundtrip-misconfigured backend=signoz")
    assert "iac_ref" in err


def test_probe_identity_names_the_service_that_declares_the_probe() -> None:
    """The canary log and the alert of the same probe carry one `infra.service.id`."""
    declaring = registry.service_attrs()[probe.SIGNOZ_PROBE_SERVICE_ID]
    facets = {facet.name: facet for facet in declaring.probes}

    assert facets[probe.SIGNOZ_PROBE_NAME].target.endswith(
        "observability_roundtrip_probe.py signoz"
    )


def test_deploy_issued_release_ref_reaches_the_canary_log(monkeypatch) -> None:
    """The deploy issues INFRA_IAC_REF, the compose hands it to the probe runner, and
    the canary log reports it. The probe runner is a separate container of the compose."""
    issued = ServiceIdentity.build(
        "platform/alerting",
        "staging",
        component="alerting",
        service_name="platform-alerting",
        version=SHA40,
        iac_ref=SHA40,
    ).deploy_env()

    runner_env = container_env(ALERTING_COMPOSE, "infra-probe-runner", issued)

    assert runner_env["INFRA_IAC_REF"] == SHA40
    resource = _signoz_resource(monkeypatch, iac_ref=runner_env["INFRA_IAC_REF"])
    assert resource["service.version"] == SHA40
    assert resource["infra.iac.ref"] == SHA40
