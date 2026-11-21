"""The ops-checks scheduler heartbeat leaves a verdict on every outcome (#1877)."""

from __future__ import annotations

import urllib.error
from pathlib import Path

from infra2_sdk.observability import load_trail

from common.observability import scheduler_heartbeat as hb

ROOT = Path(__file__).resolve().parents[2]


class _Response:
    def __init__(self, status: int, body: bytes = b'{"ok": true}') -> None:
        self.status, self._body = status, body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self._body


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {hb.VERDICTS_ENV: str(tmp_path / "verdicts.jsonl"), **extra}


def test_a_missing_token_is_a_red_verdict_and_opens_an_issue_trail_entry(
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    env = _env(tmp_path)
    assert hb.main(env, opener=lambda *a, **k: calls.append(a)) == 1
    assert calls == []
    trail = load_trail(env[hb.VERDICTS_ENV], expected_sources=())
    verdict = trail.failing[hb.HEARTBEAT_SOURCE]
    assert "INFRA2_WATCHDOG_HEARTBEAT_TOKEN is missing" in verdict.detail
    assert (verdict.severity, verdict.failure_domain) == ("P1", "ops-scheduler")


def test_a_sent_heartbeat_is_green_and_carries_the_token(tmp_path: Path) -> None:
    seen = []

    def opener(request, timeout):
        seen.append(
            (
                request.full_url,
                request.get_header("Authorization"),
                request.data,
                timeout,
            )
        )
        return _Response(200)

    env = _env(
        tmp_path,
        INFRA2_WATCHDOG_HEARTBEAT_TOKEN="t0",
        INFRA2_WATCHDOG_HEARTBEAT_URL="https://w.test/heartbeat",
    )
    assert hb.main(env, opener=opener, now=lambda: 1700000000.0) == 0
    assert len(seen) == 1 and seen[0][:2] == ("https://w.test/heartbeat", "Bearer t0")
    assert (
        b'"name": "ops-checks-scheduler"' in seen[0][2]
        and b'"timestamp": 1700000000' in seen[0][2]
    )
    trail = load_trail(env[hb.VERDICTS_ENV], expected_sources=())
    assert trail.is_green(hb.HEARTBEAT_SOURCE) and not trail.failing


def test_a_refused_or_unreachable_worker_is_red_after_three_attempts(
    tmp_path: Path,
) -> None:
    for failure in (lambda: _Response(500, b"boom"), None):
        attempts, sleeps = [], []

        def opener(request, timeout, failure=failure):
            attempts.append(1)
            if failure is None:
                raise urllib.error.URLError("no route")
            return failure()

        verdict = hb.send(
            {"INFRA2_WATCHDOG_HEARTBEAT_TOKEN": "t0"},
            opener=opener,
            sleep=sleeps.append,
        )
        assert not verdict.ok and len(attempts) == 3 and sleeps == [5, 5]
        assert "after 3 attempts" in verdict.detail and (
            "boom" in verdict.detail or "no route" in verdict.detail
        )


def test_the_url_falls_back_from_the_status_url_to_the_default() -> None:
    assert (
        hb.heartbeat_url({"INFRA2_WATCHDOG_WORKER_STATUS_URL": "https://w.test/status"})
        == "https://w.test/heartbeat"
    )
    assert hb.heartbeat_url({}) == hb.DEFAULT_URL


def test_the_workflow_step_runs_the_module_before_the_issue_trail() -> None:
    text = (ROOT / ".github/workflows/ops-checks.yml").read_text(encoding="utf-8")
    send = text.index("run: python -m common.observability.scheduler_heartbeat")
    trail = text.index("run: python tools/watchdog_issue_trail.py")
    assert send < trail
    assert "cannot send ops-checks scheduler heartbeat" not in text
