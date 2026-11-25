"""platform/30.todo/probe_health.py --verify-summary-sso: the staging gate needs SSO on the summary route.

The Canary app and its LLM summary tests live in infra2-sdk canary/todo (#247).
"""

from __future__ import annotations

import json

import pytest

from common.tests.conftest import load_repo_module


@pytest.mark.parametrize("status_code", [302, 401, 403])
def test_probe_health_verify_summary_sso_accepts_protected_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    from types import SimpleNamespace

    probe = load_repo_module("platform/30.todo/probe_health.py", "todo_probe_sso_pass")
    monkeypatch.setattr(
        probe,
        "poll_until_healthy",
        lambda *a, **kw: SimpleNamespace(
            body=json.dumps({"ok": True, "service": "platform/todo"})
        ),
    )
    observed: list[str] = []

    def _fake_summary_probe(url: str, _timeout: float) -> int:
        observed.append(url)
        return status_code

    code = probe.main(
        [
            "https://todo-staging.zitian.party",
            "--require-service",
            "platform/todo",
            "--verify-summary-sso",
        ],
        summary_probe=_fake_summary_probe,
    )
    assert code == 0
    assert observed == ["https://todo-staging.zitian.party/api/todos/summary"]


@pytest.mark.parametrize("status_code", [200, 500])
def test_probe_health_verify_summary_sso_rejects_unprotected_or_5xx(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    from types import SimpleNamespace

    probe = load_repo_module("platform/30.todo/probe_health.py", "todo_probe_sso_fail")
    monkeypatch.setattr(
        probe,
        "poll_until_healthy",
        lambda *a, **kw: SimpleNamespace(
            body=json.dumps({"ok": True, "service": "platform/todo"})
        ),
    )
    with pytest.raises(AssertionError, match="expected SSO redirect/deny"):
        probe.main(
            ["https://todo-staging.zitian.party", "--verify-summary-sso"],
            summary_probe=lambda _url, _t: status_code,
        )


def _healthy(monkeypatch: pytest.MonkeyPatch, probe) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        probe,
        "poll_until_healthy",
        lambda *a, **kw: SimpleNamespace(
            body=json.dumps({"ok": True, "service": "platform/todo"})
        ),
    )


_SSO_ARGS = [
    "https://todo-staging.zitian.party",
    "--require-service",
    "platform/todo",
    "--verify-summary-sso",
    "--retry-interval",
    "0",
]


def test_a_read_timeout_of_the_sso_probe_gets_another_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay of staging v3.2.9: one read timeout, then the SSO redirect.

    ### Mutation evidence
    - Calling ``probe`` once in ``main`` (no ``summary_sso_status``) fails this test with
      TimeoutError.
    """
    probe = load_repo_module("platform/30.todo/probe_health.py", "todo_probe_sso_retry")
    _healthy(monkeypatch, probe)
    calls: list[int] = []

    def _flaky(_url: str, _timeout: float) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        return 302

    assert probe.main(_SSO_ARGS, summary_probe=_flaky) == 0
    assert len(calls) == 2


def test_the_sso_probe_fails_after_the_last_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three timeouts fail the gate; the attempt count follows ``--attempts``."""
    import urllib.error

    probe = load_repo_module(
        "platform/30.todo/probe_health.py", "todo_probe_sso_exhaust"
    )
    _healthy(monkeypatch, probe)
    calls: list[int] = []

    def _down(_url: str, _timeout: float) -> int:
        calls.append(1)
        raise urllib.error.URLError(TimeoutError("timed out"))

    with pytest.raises(RuntimeError, match="no HTTP answer after 3 attempts"):
        probe.main(_SSO_ARGS, summary_probe=_down)
    assert len(calls) == 3
    calls.clear()
    with pytest.raises(RuntimeError, match="after 1 attempts"):
        probe.main([*_SSO_ARGS, "--attempts", "1"], summary_probe=_down)
    assert len(calls) == 1


def test_an_unprotected_status_is_an_answer_and_gets_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 means the route has no SSO: the gate fails at once, with no retry."""
    probe = load_repo_module("platform/30.todo/probe_health.py", "todo_probe_sso_200")
    _healthy(monkeypatch, probe)
    calls: list[int] = []

    def _open(_url: str, _timeout: float) -> int:
        calls.append(1)
        return 200

    with pytest.raises(AssertionError, match="returned 200"):
        probe.main(_SSO_ARGS, summary_probe=_open)
    assert len(calls) == 1
