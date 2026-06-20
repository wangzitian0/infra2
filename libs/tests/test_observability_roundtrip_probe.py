"""Tests for SigNoz/OpenPanel synthetic round-trip probes."""

from __future__ import annotations

import json
import sys

from tools import observability_roundtrip_probe as probe
from tools.openpanel_clients import OPENPANEL_CLIENTS


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
        "key": "deployment.environment",
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
