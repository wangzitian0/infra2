"""The iac_runner /deploy webhook client — signing fidelity + payload shape.

The signature the client sends MUST verify under the server's
``webhook_server.verify_iac_signature`` (HMAC over ``{ts}.{nonce}.``+payload), or every
platform deploy via deploy_v2 would 401. No network: the transport is injected.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from libs.iac_runner_client import (
    _sign,
    poll_platform_deploy_status,
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
    expected = "sha256=" + hmac.new(
        SECRET.encode(), f"{ts}.{nonce}.".encode() + payload, hashlib.sha256
    ).hexdigest()
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
    assert h["X-Hub-Signature-256"] == _sign(SECRET, "1700000000", "n" * 16, c["content"])


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
        env="staging", ref=SHA, services=["redis"], base_url="u", secret=SECRET,
        transport=transport,
    )
    base.update(kw)
    with pytest.raises(ValueError, match=match):
        trigger_platform_deploy(**base)


def test_poll_returns_on_terminal_status():
    _calls, transport = _capture([{"status": "running"}, {"status": "success"}])
    res = poll_platform_deploy_status(
        env="staging", ref=SHA, base_url="u", secret=SECRET,
        interval=0, sleep=lambda *_: None, nonce_factory=lambda: "nonce123",
        transport=transport,
    )
    assert res["status"] == "success"


def test_poll_times_out_if_never_settles():
    _calls, transport = _capture([{"status": "running"}])
    with pytest.raises(TimeoutError, match="did not settle"):
        poll_platform_deploy_status(
            env="staging", ref=SHA, base_url="u", secret=SECRET, attempts=3,
            interval=0, sleep=lambda *_: None, nonce_factory=lambda: "nonce123",
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
        env="staging", ref=SHA, base_url="u", secret=SECRET, attempts=10,
        interval=0, sleep=lambda *_: None, nonce_factory=lambda: "nonce123",
        transport=transport,
    )
    assert res["status"] == "completed"


def test_poll_times_out_if_only_not_found():
    # a deploy that never becomes visible exhausts the budget as TimeoutError (the same
    # contract as a stuck "running"), not an immediate raise.
    _calls, transport = _capture([({"status": "not_found"}, 404)])
    with pytest.raises(TimeoutError, match="did not settle"):
        poll_platform_deploy_status(
            env="staging", ref=SHA, base_url="u", secret=SECRET, attempts=3,
            interval=0, sleep=lambda *_: None, nonce_factory=lambda: "nonce123",
            transport=transport,
        )


def test_poll_raises_on_genuine_routing_404():
    # a 404 WITHOUT a not_found body (e.g. wrong URL / HTML 404) is a real error, not
    # the runner's transient not_found — it must still surface via raise_for_status().
    _calls, transport = _capture([({"error": "Not Found"}, 404)])
    with pytest.raises(httpx.HTTPStatusError):
        poll_platform_deploy_status(
            env="staging", ref=SHA, base_url="u", secret=SECRET, attempts=3,
            interval=0, sleep=lambda *_: None, nonce_factory=lambda: "nonce123",
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
