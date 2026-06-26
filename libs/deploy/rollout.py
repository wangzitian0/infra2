"""Unified Dokploy deployment-rollout poller (D5 — DRAFT proposal).

Today there are THREE near-duplicate pollers that each wait for a NEW Dokploy
deployment record to appear and settle, but with divergent contracts:

| caller                                            | returns       | success rule              | on error      | on timeout        |
|---------------------------------------------------|---------------|---------------------------|---------------|-------------------|
| ``deploy.deployer._wait_for_new_deployment_record``| ``bool``      | ``running`` is OK (a health  | raise         | return ``False``  |
|                                                   |               | check follows)            |               |                   |
| ``deploy.promote.wait_for_rollout``               | ``dict``      | poll PAST ``running`` to a   | raise         | raise TimeoutError|
|                                                   |               | terminal-good status      |               |                   |
| ``dokploy_route_canary._wait_for_new_deployment`` | ``CanaryStep``| ``running``/done is OK       | classify (no  | classify (no      |
|                                                   |               |                           | raise)        | raise)            |

This module proposes ONE poller that spans all three via three flags, so each
caller keeps its exact behaviour while sharing the loop:

- ``require_terminal``  — False: ``running``/done ends the wait (deployer, canary);
                          True: keep polling until a terminal-good status (promote).
- ``raise_on_error``    — True: raise on an ``error`` record (deployer, promote);
                          False: return a classified result (canary).
- ``raise_on_timeout``  — True: raise ``TimeoutError`` (promote);
                          False: return a ``timeout`` result (deployer, canary).

It returns a rich :class:`RolloutResult`; each caller maps that to its own return
type (bool / dict / CanaryStep). **This PR adds the poller + tests only — it does
NOT rewire the three callers.** Migrating them (and whether the deploy strategy
host is ``libs/deploy`` or the app framework — the open (a)/(b) decision) is the
discussion this draft exists for.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Dokploy deployment statuses we treat as "the rollout is progressing/succeeded".
_RUNNING_OR_DONE = {"running", "done", "success", "successful"}
_TERMINAL_GOOD = {"done", "success", "successful"}


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
    raise_on_error: bool = True,
    raise_on_timeout: bool = False,
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
    while True:
        attempts += 1
        deployments = get_deployments() or []
        current_ids = {_deployment_id(d) for d in deployments if _deployment_id(d)}
        new_ids = current_ids - before_ids
        if new_ids:
            latest = _newest(deployments, new_ids)
            status = str(latest.get("status") or "").lower()
            ids = tuple(sorted(new_ids))
            if status == "error":
                if raise_on_error:
                    raise RolloutError("Dokploy deployment record entered error")
                return RolloutResult("error", latest, ids, attempts)
            terminal = status in _TERMINAL_GOOD
            progressing = status in _RUNNING_OR_DONE
            if (require_terminal and terminal) or (
                not require_terminal and progressing
            ):
                return RolloutResult(
                    "done" if terminal else "running", latest, ids, attempts
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
