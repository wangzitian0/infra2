"""The ops-checks scheduler heartbeat leaves a verdict on every run (#1877)."""

from __future__ import annotations

import http.client
import json
import urllib.error
from pathlib import Path

import pytest
from infra2_sdk.observability import load_trail

from common.observability import scheduler_heartbeat as hb
from common.observability.watchdog_issue_trail_cli import (
    EXPECTED_SOURCES,
    HEARTBEAT_CHECK,
    HEARTBEAT_SOURCE,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEDULED = {"GITHUB_EVENT_NAME": "schedule"}


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
    return {hb.VERDICTS_ENV: str(tmp_path / "verdicts.jsonl"), **SCHEDULED, **extra}


def _never(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("no request may leave on this path")


def test_a_missing_token_is_a_red_verdict_in_the_issue_trail(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert hb.main(env, opener=_never) == 1
    verdict = load_trail(env[hb.VERDICTS_ENV], expected_sources=()).failing[
        HEARTBEAT_CHECK
    ]
    assert verdict.detail == hb.MISSING_TOKEN
    assert (verdict.severity, verdict.failure_domain, verdict.skipped) == (
        "P1",
        "ops-scheduler",
        False,
    )


def test_a_sent_heartbeat_is_green_and_has_the_contract_of_the_worker(
    tmp_path: Path,
) -> None:
    seen = []

    def opener(request, timeout):
        seen.append((request, timeout))
        return _Response(200)

    env = _env(
        tmp_path,
        INFRA2_WATCHDOG_HEARTBEAT_TOKEN="t0",
        INFRA2_WATCHDOG_HEARTBEAT_URL="https://w.test/heartbeat",
    )
    assert hb.main(env, opener=opener, now=lambda: 1700000000.9) == 0
    ((request, timeout),) = seen
    assert (request.full_url, request.get_method(), timeout) == (
        "https://w.test/heartbeat",
        "POST",
        15,
    )
    assert dict(request.header_items()) == {
        "Authorization": "Bearer t0",
        "Content-type": "application/json",
        "User-agent": "infra2-ops-checks/1.0",
    }
    assert json.loads(request.data) == {
        "env": "production",
        "name": "ops-checks-scheduler",
        "ok": True,
        "detail": "ops-checks watchdog scheduler tick",
        "timestamp": 1700000000,
    }
    trail = load_trail(env[hb.VERDICTS_ENV], expected_sources=())
    assert trail.is_green(HEARTBEAT_CHECK) and not trail.failing and not trail.skipped


@pytest.mark.parametrize(
    ("failure", "needle"),
    [
        (lambda: _Response(500, b"boom"), "HTTP 500: boom"),
        (urllib.error.URLError("no route"), "no route"),
        (TimeoutError("slow"), "TimeoutError: slow"),
        (http.client.BadStatusLine("junk"), "BadStatusLine"),
        (http.client.IncompleteRead(b""), "IncompleteRead"),
        (
            ValueError("Invalid header value b'Bearer t0-secret'"),
            "Invalid header value",
        ),
    ],
)
def test_each_failed_send_is_a_red_verdict_after_three_attempts(
    failure, needle
) -> None:
    attempts, sleeps = [], []

    def opener(request, timeout):
        attempts.append(1)
        if isinstance(failure, Exception):
            raise failure
        return failure()

    verdict = hb.send(
        {"INFRA2_WATCHDOG_HEARTBEAT_TOKEN": "t0-secret"},
        opener=opener,
        sleep=sleeps.append,
    )
    assert not verdict.ok and not verdict.skipped
    assert len(attempts) == 3
    assert sleeps == [5, 5]
    assert verdict.detail.startswith(
        "ops-checks scheduler heartbeat failed after 3 attempts: "
    )
    assert needle in verdict.detail
    assert "t0-secret" not in verdict.detail


def test_a_bad_url_is_a_red_verdict_and_not_a_crash() -> None:
    env = {
        "INFRA2_WATCHDOG_HEARTBEAT_TOKEN": "t0",
        "INFRA2_WATCHDOG_HEARTBEAT_URL": "not a url",
    }
    verdict = hb.send(env, opener=_never, sleep=lambda _s: None)
    assert not verdict.ok
    assert "unknown url type" in verdict.detail


def test_a_manual_run_sends_nothing_and_records_a_skipped_verdict(
    tmp_path: Path,
) -> None:
    env = {
        hb.VERDICTS_ENV: str(tmp_path / "verdicts.jsonl"),
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "INFRA2_WATCHDOG_HEARTBEAT_TOKEN": "t0",
    }
    assert hb.main(env, opener=_never) == 0
    trail = load_trail(env[hb.VERDICTS_ENV], expected_sources=())
    assert trail.skipped == {HEARTBEAT_CHECK}
    assert not trail.failing
    # A skipped check is neither green nor retired: an open heartbeat issue stays open.
    trail.complete = True
    assert not trail.is_green(HEARTBEAT_CHECK)
    assert not trail.is_retired(HEARTBEAT_CHECK)
    assert HEARTBEAT_CHECK != HEARTBEAT_SOURCE


def test_a_step_that_recorded_nothing_is_red_in_the_trail(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.jsonl"
    path.write_text("", encoding="utf-8")
    assert HEARTBEAT_SOURCE in EXPECTED_SOURCES
    crashed = load_trail(path, expected_sources=EXPECTED_SOURCES)
    assert "recorded no verdict" in crashed.failing[HEARTBEAT_SOURCE].detail


def test_an_unwritable_verdict_path_keeps_the_exit_code(tmp_path: Path, capsys) -> None:
    env = {
        hb.VERDICTS_ENV: str(tmp_path / "absent-dir" / "verdicts.jsonl"),
        **SCHEDULED,
    }
    assert hb.main(env, opener=_never) == 1
    assert "could not record the heartbeat verdict" in capsys.readouterr().err


def test_the_url_falls_back_from_the_status_url_to_the_default() -> None:
    assert (
        hb.heartbeat_url({"INFRA2_WATCHDOG_WORKER_STATUS_URL": "https://w.test/status"})
        == "https://w.test/heartbeat"
    )
    assert (
        hb.heartbeat_url({"INFRA2_WATCHDOG_HEARTBEAT_URL": " https://x.test/h "})
        == "https://x.test/h"
    )
    assert (
        hb.heartbeat_url({})
        == hb.DEFAULT_URL
        == "https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/heartbeat"
    )


def test_the_issue_trail_cli_expects_the_heartbeat_source() -> None:
    text = (ROOT / "common/observability/watchdog_issue_trail_cli.py").read_text(
        encoding="utf-8"
    )
    assert "load_trail(path, expected_sources=EXPECTED_SOURCES)" in text
