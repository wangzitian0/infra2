"""Unit tests for common/deploy/rollout.py — the unified deployment poller (D5 draft).

Each test pins one of the three existing pollers' contracts onto the unified
``wait_for_deployment`` flags, proving the single loop can reproduce all of them.
"""

import pytest

from common.deploy.rollout import RolloutError, wait_for_deployment


class _Deployments:
    """Returns the next queued deployment-list per call; the last entry repeats."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.calls = 0

    def __call__(self):
        i = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return self._frames[i]


def _no_sleep(_):  # deterministic, no real waiting
    return None


def _clock(ticks):
    """A monotonic() stub that yields successive values then holds the last."""
    seq = list(ticks)

    def now():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return now


# --- deployer contract: running is success, no-raise on timeout ---------------


def test_running_is_success_when_not_requiring_terminal():
    deployments = _Deployments([[{"id": "d2", "status": "running"}]])
    result = wait_for_deployment(
        deployments,
        before_ids={"d1"},
        timeout_seconds=30,
        interval_seconds=1,
        require_terminal=False,
        _sleep=_no_sleep,
    )
    assert result.status == "running"
    assert result.ok is True
    assert result.new_ids == ("d2",)


# --- promote contract: poll PAST running to terminal-good ---------------------


def test_require_terminal_polls_past_running_to_done():
    deployments = _Deployments(
        [
            [{"id": "d2", "status": "running"}],
            [{"id": "d2", "status": "done"}],
        ]
    )
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=30,
        interval_seconds=1,
        require_terminal=True,
        _sleep=_no_sleep,
    )
    assert result.status == "done"
    assert result.ok is True
    assert deployments.calls >= 2  # it kept polling past `running`


# --- error handling: raise (deployer/promote) vs classify (canary) ------------


def test_error_status_raises_when_raise_on_error():
    deployments = _Deployments(
        [[{"id": "d2", "status": "error", "createdAt": "2026-09-01T00:00:00Z"}]]
    )
    with pytest.raises(RolloutError) as exc_info:
        wait_for_deployment(
            deployments,
            before_ids=set(),
            timeout_seconds=30,
            interval_seconds=1,
            raise_on_error=True,
            _sleep=_no_sleep,
        )
    msg = str(exc_info.value)
    assert "id=d2" in msg
    assert "status=error" in msg
    assert "createdAt=2026-09-01T00:00:00Z" in msg
    assert "new_ids=['d2']" in msg


def test_error_status_classified_when_not_raising():
    deployments = _Deployments([[{"id": "d2", "status": "error"}]])
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=30,
        interval_seconds=1,
        raise_on_error=False,
        _sleep=_no_sleep,
    )
    assert result.status == "error"
    assert result.ok is False


# --- timeout: raise (promote) vs classify (deployer/canary) -------------------


def test_timeout_raises_when_raise_on_timeout():
    deployments = _Deployments(
        [[{"id": "d3", "status": "running", "createdAt": "2026-09-01T01:00:00Z"}]]
    )
    with pytest.raises(TimeoutError) as exc_info:
        wait_for_deployment(
            deployments,
            before_ids=set(),
            timeout_seconds=0,
            interval_seconds=1,
            require_terminal=True,
            raise_on_timeout=True,
            _sleep=_no_sleep,
            _now=_clock([0.0, 1.0]),
        )
    msg = str(exc_info.value)
    assert "latest_id=d3" in msg
    assert "status=running" in msg
    assert "createdAt=2026-09-01T01:00:00Z" in msg
    assert "new_ids=['d3']" in msg
    assert "attempts=1" in msg


def test_timeout_classified_when_not_raising():
    deployments = _Deployments([[]])
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=0,
        interval_seconds=1,
        raise_on_timeout=False,
        _sleep=_no_sleep,
        _now=_clock([0.0, 1.0]),
    )
    assert result.status == "timeout"
    assert result.ok is False


# --- only NEW records count ---------------------------------------------------


def test_pre_existing_record_is_ignored():
    # d1 is already done before the deploy, but it's in before_ids -> not success
    deployments = _Deployments([[{"id": "d1", "status": "done"}]])
    result = wait_for_deployment(
        deployments,
        before_ids={"d1"},
        timeout_seconds=0,
        interval_seconds=1,
        raise_on_timeout=False,
        _sleep=_no_sleep,
        _now=_clock([0.0, 1.0]),
    )
    assert result.status == "timeout"  # no NEW record, so it times out


def test_filtered_record_is_ignored():
    # d2 is new and done, but filtered by predicate -> not considered our deployment
    deployments = _Deployments([[{"id": "d2", "status": "done", "skip": True}]])
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=0,
        interval_seconds=1,
        raise_on_timeout=False,
        is_filtered_fn=lambda d: bool(d.get("skip")),
        _sleep=_no_sleep,
        _now=_clock([0.0, 1.0]),
    )
    assert result.status == "timeout"


def test_custom_terminal_success_statuses():
    # Caller supplies custom terminal statuses (e.g. 'completed')
    deployments = _Deployments([[{"id": "d2", "status": "completed"}]])
    result = wait_for_deployment(
        deployments,
        before_ids=set(),
        timeout_seconds=30,
        interval_seconds=1,
        require_terminal=True,
        terminal_success_statuses={"completed"},
        _sleep=_no_sleep,
    )
    assert result.status == "done"
    assert result.ok is True


def test_deploy_compose_with_record_check_retries_once_on_runtime_error() -> None:
    """#1416 (G1): when the initial deploy_compose waiter raises RuntimeError (e.g. a
    transient GHCR DNS lookup error in Dokploy), deploy_compose_with_record_check warns,
    sleeps once, and retries with client.redeploy_compose."""
    from common.deploy.dokploy_adapter import deploy_compose_with_record_check

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_compose_deployments(self, _cid: str) -> list[dict]:
            return [{"deploymentId": "old-1"}]

        def deploy_compose(self, cid: str) -> None:
            self.calls.append(f"deploy:{cid}")

        def redeploy_compose(self, cid: str) -> None:
            self.calls.append(f"redeploy:{cid}")

    client = _FakeClient()
    warnings: list[str] = []
    sleeps: list[float] = []
    attempts = 0

    def _flaky_waiter(_c, _cid, _prev, _to, _iv, _min_s) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(
                "Dokploy deployment record entered error (deploymentId=dep-1, "
                "log_tail=lookup ghcr.io on 127.0.0.53:53: server misbehaving)"
            )
        return True

    deploy_compose_with_record_check(
        client,
        "compose-123",
        interval_seconds=3,
        wait_fn=_flaky_waiter,
        log_warning=warnings.append,
        sleep_fn=sleeps.append,
    )
    assert client.calls == ["deploy:compose-123", "redeploy:compose-123"]
    assert len(warnings) == 1
    assert "retrying once with compose.redeploy" in warnings[0]
    assert sleeps == [3]

    # When both deploy_compose and redeploy_compose raise RuntimeError, the second error propagates.
    def _always_failing_waiter(*_a) -> bool:
        raise RuntimeError("Dokploy deployment record entered error (persistent)")

    with pytest.raises(RuntimeError, match="persistent"):
        deploy_compose_with_record_check(
            client,
            "compose-123",
            interval_seconds=2,
            wait_fn=_always_failing_waiter,
            log_warning=warnings.append,
            sleep_fn=sleeps.append,
        )


def test_ssh_args_for_log_tail_uses_runtime_ssh_key_and_accept_new(
    tmp_path, monkeypatch
) -> None:
    """#1416 (G1): _ssh_args_for_log_tail falls back to /root/.ssh_runtime/id_ed25519
    when INFRA2_WATCHDOG_SSH_KEY_PATH is unset, and uses StrictHostKeyChecking=accept-new."""
    from common.deploy.dokploy_adapter import _ssh_args_for_log_tail

    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_KEY_PATH", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_PORT", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_USER", raising=False)
    runtime_key = tmp_path / "id_ed25519"
    runtime_key.write_text("dummy-key", encoding="utf-8")

    args = _ssh_args_for_log_tail("10.0.0.1", runtime_key_path=runtime_key)
    assert "StrictHostKeyChecking=accept-new" in args
    assert "StrictHostKeyChecking=no" not in args
    assert "-i" in args
    assert args[args.index("-i") + 1] == str(runtime_key)
    assert args[-1] == "root@10.0.0.1"
