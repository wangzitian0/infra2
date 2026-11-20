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
