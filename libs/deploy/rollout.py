"""Unified Dokploy deployment rollout poller.

This module provides one poller that replaces duplicate polling implementations:

- libs.deploy.dokploy_adapter.wait_for_new_deployment_record
- libs.deploy.promote.wait_for_rollout

Flags reproduce each caller contract:

- require_terminal: False allows progressing status. True requires terminal status.
- raise_on_error: True raises on error status. False returns classified result.
- raise_on_timeout: True raises TimeoutError. False returns timeout result.
- is_filtered_fn: Optional predicate to exclude concurrent unrelated deployment records.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Dokploy deployment statuses we treat as "the rollout is progressing/succeeded".
TERMINAL_SUCCESS_STATUSES = frozenset({"done", "success", "successful"})
_RUNNING_OR_DONE = frozenset({"running", "done", "success", "successful"})
_TERMINAL_GOOD = TERMINAL_SUCCESS_STATUSES


class RolloutError(RuntimeError):
    """A new deployment record entered an ``error`` status."""


@dataclass(frozen=True)
class RolloutResult:
    """Outcome of waiting for a new Dokploy deployment record.

    ``status`` is one of: ``running`` | ``done`` | ``error`` | ``timeout``.
    ``deployment`` is the newest NEW deployment record observed (or ``{}``).
    """

    status: str
    deployment: dict[str, Any] = field(default_factory=dict)
    new_ids: tuple[str, ...] = ()
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {"running", "done"}


def _deployment_id(d: dict[str, Any]) -> str:
    return str(d.get("deploymentId") or d.get("id") or "")


def _newest(deployments: list[dict[str, Any]], ids: set[str]) -> dict[str, Any]:
    candidates = [d for d in deployments if _deployment_id(d) in ids]
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda d: str(d.get("createdAt") or d.get("startedAt") or ""),
    )


def wait_for_deployment(
    get_deployments: Callable[[], list[dict[str, Any]]],
    before_ids: set[str],
    *,
    timeout_seconds: int,
    interval_seconds: int,
    require_terminal: bool = False,
    terminal_success_statuses: set[str] | frozenset[str] | None = None,
    raise_on_error: bool = True,
    raise_on_timeout: bool = False,
    is_filtered_fn: Callable[[dict[str, Any]], bool] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _now: Callable[[], float] = time.monotonic,
) -> RolloutResult:
    """Poll ``get_deployments`` until a NEW deployment record settles.

    ``get_deployments`` returns the compose's current deployment records each call
    (the caller injects how to fetch them, so this stays client-agnostic). See the
    module docstring for how the three flags reproduce each existing poller.
    """
    deadline = _now() + max(0, timeout_seconds)
    attempts = 0
    terminal_statuses = (
        set(terminal_success_statuses)
        if terminal_success_statuses is not None
        else _TERMINAL_GOOD
    )
    while True:
        attempts += 1
        raw_deployments = get_deployments() or []
        if is_filtered_fn is not None:
            deployments = [d for d in raw_deployments if not is_filtered_fn(d)]
        else:
            deployments = raw_deployments
        current_ids = {_deployment_id(d) for d in deployments if _deployment_id(d)}
        new_ids = current_ids - before_ids
        if new_ids:
            new_records = [d for d in deployments if _deployment_id(d) in new_ids]
            for d in new_records:
                status = str(d.get("status") or "").lower()
                if status == "error":
                    if raise_on_error:
                        raise RolloutError("Dokploy deployment record entered error")
                    return RolloutResult("error", d, tuple(sorted(new_ids)), attempts)

            terminal_records = [
                d
                for d in new_records
                if str(d.get("status") or "").lower() in terminal_statuses
            ]
            if terminal_records:
                latest = _newest(terminal_records, new_ids)
                return RolloutResult("done", latest, tuple(sorted(new_ids)), attempts)

            if not require_terminal:
                progressing_records = [
                    d
                    for d in new_records
                    if str(d.get("status") or "").lower() in _RUNNING_OR_DONE
                ]
                if progressing_records:
                    latest = _newest(progressing_records, new_ids)
                    return RolloutResult(
                        "running", latest, tuple(sorted(new_ids)), attempts
                    )

        if _now() >= deadline:
            if raise_on_timeout:
                raise TimeoutError(
                    "no new deployment reached a terminal status in the window"
                )
            return RolloutResult(
                "timeout",
                _newest(deployments, new_ids) if new_ids else {},
                tuple(sorted(new_ids)),
                attempts,
            )
        _sleep(interval_seconds)
