"""The iac_runner /deploy webhook client — signing fidelity + payload shape.

The signature the client sends MUST verify under the server's
``webhook_server.verify_iac_signature`` (HMAC over ``{ts}.{nonce}.``+payload), or every
platform deploy via deploy_v2 would 401. No network: the transport is injected.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import math

import httpx
import pytest

from libs.iac_runner_client import (
    _sign,
    poll_platform_deploy_status,
    status_poll_attempts,
    status_poll_delays,
    trigger_platform_deploy,
)

SECRET = "test-secret"
SHA = "a" * 40


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("POST", "http://x/deploy/status"),
                response=httpx.Response(self.status_code),
            )
        return None

    def json(self):
        return self._payload


def _capture(responses=None):
    calls = []
    seq = list(responses or [{"status": "success", "deployment_id": "d1"}])

    def transport(url, *, content, headers, timeout):
        calls.append({"url": url, "content": content, "headers": headers})
        # each item is either a payload dict or a (payload, status_code) tuple
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, tuple):
            payload, code = item
            return _FakeResp(payload, status_code=code)
        return _FakeResp(item)

    return calls, transport


def test_sign_matches_server_hmac_formula():
    ts, nonce, payload = "1700000000", "abc123def456", b'{"x":1}'
    expected = (
        "sha256="
        + hmac.new(
            SECRET.encode(), f"{ts}.{nonce}.".encode() + payload, hashlib.sha256
        ).hexdigest()
    )
    assert _sign(SECRET, ts, nonce, payload) == expected


def test_trigger_builds_signed_payload_and_posts_to_deploy():
    calls, transport = _capture()
    res = trigger_platform_deploy(
        env="staging",
        ref=SHA,
        services=["redis"],
        base_url="https://iac.example/",
        secret=SECRET,
        now=lambda: 1700000000.0,
        nonce="n" * 16,
        transport=transport,
    )
    assert res["status"] == "success"
    c = calls[0]
    assert c["url"] == "https://iac.example/deploy"  # trailing slash normalized
    assert json.loads(c["content"]) == {
        "env": "staging",
        "ref": SHA,
        "triggered_by": "deploy_v2",
        "wait": False,
        "services": ["redis"],
    }
    h = c["headers"]
    assert h["X-IAC-Timestamp"] == "1700000000"
    assert h["X-IAC-Nonce"] == "n" * 16
    # the signature is over {ts}.{nonce}.+payload — exactly what the server recomputes
    assert h["X-Hub-Signature-256"] == _sign(
        SECRET, "1700000000", "n" * 16, c["content"]
    )


def test_trigger_normalizes_service_set_before_signing():
    calls, transport = _capture()

    trigger_platform_deploy(
        env="staging",
        ref=SHA,
        services=[" redis ", "postgres", "redis"],
        base_url="https://iac.example",
        secret=SECRET,
        transport=transport,
    )

    assert json.loads(calls[0]["content"])["services"] == ["postgres", "redis"]


@pytest.mark.parametrize(
    "kw,match",
    [
        (dict(env="prod"), "env must be"),  # webhook env is 'production', not 'prod'
        (dict(ref="main"), "40-hex"),
        (dict(secret=""), "SECRET"),
        (dict(base_url=""), "base_url"),
    ],
)
def test_trigger_validates_inputs_before_post(kw, match):
    _, transport = _capture()
    base = dict(
        env="staging",
        ref=SHA,
        services=["redis"],
        base_url="u",
        secret=SECRET,
        transport=transport,
    )
    base.update(kw)
    with pytest.raises(ValueError, match=match):
        trigger_platform_deploy(**base)


def test_poll_returns_on_terminal_status():
    calls, transport = _capture([{"status": "running"}, {"status": "success"}])
    res = poll_platform_deploy_status(
        env="staging",
        ref=SHA,
        services=["redis"],
        deployment_id="a" * 16,
        base_url="u",
        secret=SECRET,
        interval=0,
        sleep=lambda *_: None,
        nonce_factory=lambda: "nonce123",
        transport=transport,
    )
    assert res["status"] == "success"
    assert json.loads(calls[0]["content"]) == {
        "env": "staging",
        "ref": SHA,
        "triggered_by": "deploy_v2",
        "services": ["redis"],
        "deployment_id": "a" * 16,
    }


def test_poll_times_out_if_never_settles():
    _calls, transport = _capture([{"status": "running"}])
    with pytest.raises(TimeoutError, match="did not settle"):
        poll_platform_deploy_status(
            env="staging",
            ref=SHA,
            base_url="u",
            secret=SECRET,
            attempts=3,
            interval=0,
            sleep=lambda *_: None,
            nonce_factory=lambda: "nonce123",
            transport=transport,
        )


def test_poll_tolerates_transient_not_found_then_settles():
    # 404 {"status":"not_found"} right after firing (wait=False) = deploy not visible yet
    # or runner restarted and lost in-memory state. Must keep polling, not crash on the
    # first miss — this is exactly the v1.1.16 reconcile failure.
    _calls, transport = _capture(
        [
            ({"status": "not_found"}, 404),
            ({"status": "not_found"}, 404),
            ({"status": "completed"}, 200),
        ]
    )
    res = poll_platform_deploy_status(
        env="staging",
        ref=SHA,
        base_url="u",
        secret=SECRET,
        attempts=10,
        interval=0,
        sleep=lambda *_: None,
        nonce_factory=lambda: "nonce123",
        transport=transport,
    )
    assert res["status"] == "completed"


def test_poll_times_out_if_only_not_found():
    # a deploy that never becomes visible exhausts the budget as TimeoutError (the same
    # contract as a stuck "running"), not an immediate raise.
    _calls, transport = _capture([({"status": "not_found"}, 404)])
    with pytest.raises(TimeoutError, match="did not settle"):
        poll_platform_deploy_status(
            env="staging",
            ref=SHA,
            base_url="u",
            secret=SECRET,
            attempts=3,
            interval=0,
            sleep=lambda *_: None,
            nonce_factory=lambda: "nonce123",
            transport=transport,
        )


def test_poll_raises_on_genuine_routing_404():
    # a 404 WITHOUT a not_found body (e.g. wrong URL / HTML 404) is a real error, not
    # the runner's transient not_found — it must still surface via raise_for_status().
    _calls, transport = _capture([({"error": "Not Found"}, 404)])
    with pytest.raises(httpx.HTTPStatusError):
        poll_platform_deploy_status(
            env="staging",
            ref=SHA,
            base_url="u",
            secret=SECRET,
            attempts=3,
            interval=0,
            sleep=lambda *_: None,
            nonce_factory=lambda: "nonce123",
            transport=transport,
        )


def test_poll_validates_inputs_before_post():
    _, transport = _capture()
    with pytest.raises(ValueError, match="40-hex"):
        poll_platform_deploy_status(
            env="staging", ref="main", base_url="u", secret=SECRET, transport=transport
        )
    with pytest.raises(ValueError, match="SECRET"):
        poll_platform_deploy_status(
            env="staging", ref=SHA, base_url="u", secret="", transport=transport
        )
    with pytest.raises(ValueError, match="deployment_id"):
        poll_platform_deploy_status(
            env="staging",
            ref=SHA,
            deployment_id="not-an-id",
            base_url="u",
            secret=SECRET,
            transport=transport,
        )


def test_trigger_carries_a_validated_version_ref_when_given():
    """truealpha#712: the app release a digest-pinned platform service should pin rides
    in the payload — and only there; an absent version_ref leaves the payload byte-identical
    to before, so every other platform deploy is untouched."""
    calls, transport = _capture()
    trigger_platform_deploy(
        env="production",
        ref=SHA,
        services=["truealpha/data_engine"],
        base_url="https://iac.example",
        secret=SECRET,
        now=lambda: 1700000000.0,
        nonce="n" * 16,
        transport=transport,
        version_ref="v0.0.46",
    )
    assert json.loads(calls[0]["content"])["version_ref"] == "v0.0.46"
    calls, transport = _capture()
    trigger_platform_deploy(
        env="production",
        ref=SHA,
        services=["truealpha/data_engine"],
        base_url="https://iac.example",
        secret=SECRET,
        now=lambda: 1700000000.0,
        nonce="n" * 16,
        transport=transport,
    )
    assert "version_ref" not in json.loads(calls[0]["content"])


def test_version_ref_is_a_tag_or_a_sha_before_any_post():
    calls, transport = _capture()
    with pytest.raises(ValueError, match="vX.Y.Z tag or a 7-40 hex commit sha"):
        trigger_platform_deploy(
            env="production",
            ref=SHA,
            services=["truealpha/data_engine"],
            base_url="https://iac.example",
            secret=SECRET,
            transport=transport,
            version_ref="main; rm -rf /",
        )
    assert calls == []


def test_trigger_and_poll_carry_a_secrets_supply_action():
    """deploy_v2 asks the runner for the secret supply alone before an app stack's
    Dokploy promote (#649); the action rides in the signed body and the status coordinate."""
    calls, transport = _capture()
    trigger_platform_deploy(
        env="staging",
        ref=SHA,
        services=["finance_report/app"],
        base_url="https://iac.example/",
        secret=SECRET,
        now=lambda: 1700000000.0,
        nonce="n" * 16,
        transport=transport,
        action="secrets-supply",
    )
    assert json.loads(calls[0]["content"])["action"] == "secrets-supply"
    # the default action is not serialized: legacy runners keep their exact payload
    calls2, transport2 = _capture()
    trigger_platform_deploy(
        env="staging",
        ref=SHA,
        services=["finance_report/app"],
        base_url="https://iac.example/",
        secret=SECRET,
        now=lambda: 1700000000.0,
        nonce="n" * 16,
        transport=transport2,
    )
    assert "action" not in json.loads(calls2[0]["content"])


def test_poll_survives_a_runner_recreate_inside_the_grace_window():
    """#666: while the runner container is recreated Traefik answers a bodiless 404, then
    502/503/504 or Cloudflare 522; inside the grace window the poll keeps going and the deploy settles."""
    _calls, transport = _capture(
        [
            ({"status": "running"}, 200),
            ("404 page not found", 404),
            ({}, 502),
            ({}, 503),
            ({}, 504),
            ({}, 522),
            ({"status": "running"}, 200),
            ({"status": "completed"}, 200),
        ]
    )
    clock = iter(range(0, 1000, 10))
    res = poll_platform_deploy_status(
        env="staging",
        ref=SHA,
        base_url="u",
        secret=SECRET,
        attempts=20,
        interval=0,
        now=lambda: next(clock),
        sleep=lambda *_: None,
        nonce_factory=lambda: "nonce123",
        transport=transport,
        gateway_grace=180.0,
    )
    assert res["status"] == "completed"


def test_poll_survives_transport_timeout_inside_grace_window():
    """Cloudflare transpacific read timeout / connection drops within grace window must be retried."""
    attempts = [0]

    def buggy_transport(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise httpx.ReadTimeout("The read operation timed out")
        req = httpx.Request("POST", "http://test")
        return httpx.Response(200, json={"status": "completed"}, request=req)

    clock = iter(range(0, 1000, 10))
    res = poll_platform_deploy_status(
        env="staging",
        ref=SHA,
        base_url="u",
        secret=SECRET,
        attempts=5,
        interval=0,
        now=lambda: next(clock),
        sleep=lambda *_: None,
        nonce_factory=lambda: "nonce123",
        transport=buggy_transport,
        gateway_grace=180.0,
    )
    assert res["status"] == "completed"
    assert attempts[0] == 2


def test_poll_fails_naming_the_restart_when_the_gateway_stays_down_past_the_grace():
    _calls, transport = _capture([({}, 502)])
    clock = iter(range(0, 100000, 100))
    with pytest.raises(RuntimeError, match="recreated by a bootstrap push mid-deploy"):
        poll_platform_deploy_status(
            env="staging",
            ref=SHA,
            base_url="u",
            secret=SECRET,
            attempts=50,
            interval=0,
            now=lambda: next(clock),
            sleep=lambda *_: None,
            nonce_factory=lambda: "nonce123",
            transport=transport,
            gateway_grace=180.0,
        )


def test_poll_raises_on_a_404_that_carries_an_empty_json_object():
    """#666 review: `{}` is a JSON object the runner answered, not Traefik's bodiless
    404 — it must raise as a routing error, not enter the gateway grace window."""
    _calls, transport = _capture([({}, 404)])
    with pytest.raises(httpx.HTTPStatusError):
        poll_platform_deploy_status(
            env="staging",
            ref=SHA,
            base_url="u",
            secret=SECRET,
            attempts=3,
            interval=0,
            sleep=lambda *_: None,
            nonce_factory=lambda: "nonce123",
            transport=transport,
        )


# --- truealpha#860: the /deploy/status schedule starts short and grows to the old 10 s --


def _poll_sleeps(responses, **kw):
    slept: list[float] = []
    calls, transport = _capture(responses)
    result = poll_platform_deploy_status(
        env="staging",
        ref=SHA,
        base_url="u",
        secret=SECRET,
        sleep=slept.append,
        nonce_factory=lambda: "nonce123",
        transport=transport,
        **kw,
    )
    return result, slept, calls


def test_poll_pauses_grow_from_the_first_interval_to_the_cap():
    result, slept, calls = _poll_sleeps(
        [{"status": "running"}] * 10 + [{"status": "completed"}],
        attempts=20,
        interval=2.0,
        backoff=1.25,
        max_interval=10.0,
    )
    assert result["status"] == "completed"
    assert len(calls) == 11
    assert slept == [2.0, 2.5, 3.125, 3.90625, 4.8828125] + [
        pytest.approx(6.103515625),
        pytest.approx(7.62939453125),
        pytest.approx(9.5367431640625),
        10.0,
        10.0,
    ]


def test_poll_without_a_schedule_keeps_its_fixed_interval():
    """Every existing caller that passes only ``interval`` polls exactly as before."""
    _result, slept, _calls = _poll_sleeps(
        [{"status": "running"}] * 3 + [{"status": "completed"}], interval=10.0
    )
    assert slept == [10.0, 10.0, 10.0]


def test_poll_schedule_also_paces_not_found_and_gateway_retries():
    _result, slept, _calls = _poll_sleeps(
        [
            ({"status": "not_found"}, 404),
            ({}, 502),
            ({"status": "running"}, 200),
            ({"status": "completed"}, 200),
        ],
        interval=2.0,
        backoff=2.0,
        max_interval=5.0,
    )
    assert slept == [2.0, 4.0, 5.0]


@pytest.mark.parametrize(
    "schedule",
    [dict(backoff=0.5), dict(interval=-1.0), dict(max_interval=-1.0)],
)
def test_poll_rejects_a_bad_schedule_before_any_request(schedule):
    calls, transport = _capture()
    kw = dict(interval=2.0, backoff=1.25, max_interval=10.0)
    kw.update(schedule)
    with pytest.raises(ValueError, match="poll delays need"):
        poll_platform_deploy_status(
            env="staging",
            ref=SHA,
            base_url="u",
            secret=SECRET,
            sleep=lambda *_: None,
            transport=transport,
            **kw,
        )
    assert calls == []


@pytest.mark.parametrize("budget", [0, 1, 9, 10, 11, 120, 599, 600, 900])
def test_status_poll_attempts_on_a_fixed_schedule_is_the_old_ceiling(budget):
    assert status_poll_attempts(budget, initial=10, backoff=1, maximum=10) == max(
        1, math.ceil(budget / 10)
    )


@pytest.mark.parametrize("budget", [1, 7.1, 60, 120, 600, 900])
def test_status_poll_attempts_is_the_fewest_polls_covering_the_budget(budget):
    attempts = status_poll_attempts(budget)
    pauses = list(itertools.islice(status_poll_delays(), attempts))
    assert sum(pauses) >= budget > sum(pauses[:-1])
    # the short start costs at most five polls over the old fixed 10 s count
    assert attempts - max(1, math.ceil(budget / 10)) <= 5


def test_status_poll_attempts_refuses_a_schedule_that_never_pauses():
    with pytest.raises(ValueError, match="never pauses"):
        status_poll_attempts(60, initial=0)
