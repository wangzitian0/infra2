#!/usr/bin/env python3
"""Timestamped one-line phase markers for the deploy_v2 / promote critical path.

Measured 2026-09-15/16 (infra2 runs 34952943059, 34958278004, 34961124739,
35055657512 — receivers for truealpha staging deploys): the `execute` step of
app-deploy-request.yml takes 112-199s, but its Python subprocess's stdout is
buffered — both of `tools/app_deploy_request.py`'s result JSON lines print within
2ms of each other AT PROCESS EXIT, so nobody watching the live Actions log can tell
whether those 112-199s went to secrets-supply, the Dokploy trigger, the rollout
wait, config-hash verification, in-service verification, or the companion deploy.

This module is the additive fix for visibility only — it changes no control flow
and no return value anywhere it is called. Paired with `PYTHONUNBUFFERED=1` on the
workflow's `canary` and `execute` steps (so Python flushes eagerly instead of
buffering until exit), `phase(...)` calls become one-line, timestamped, near-real-time
progress markers in the Actions log: `[+12.34s] secrets-supply: start`.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

__all__ = ["phase", "reset_phase_clock"]

_start = time.monotonic()


def reset_phase_clock(
    now: float | None = None, *, _monotonic: Callable[[], float] = time.monotonic
) -> None:
    """Reset the zero point elapsed times are measured from.

    Test-only knob (also handy for a long-lived caller that wants a fresh t=0 at the
    start of a specific deploy) — production code never needs to call this; the
    module's own import time is a perfectly good zero point for a one-shot CLI run.
    """
    global _start
    _start = now if now is not None else _monotonic()


def phase(
    message: str,
    *,
    _now: Callable[[], float] = time.monotonic,
    _stream=None,
) -> None:
    """Print one flushed, timestamped phase marker: ``[+<elapsed>s] <message>``.

    Elapsed is monotonic seconds since this module was imported (or since the last
    `reset_phase_clock()` call). Always explicitly flushed so the marker is visible
    immediately even if a caller's own stdout happens to be buffered for some other
    reason — belt-and-suspenders alongside the workflow's `PYTHONUNBUFFERED=1`.
    """
    elapsed = _now() - _start
    stream = _stream if _stream is not None else sys.stdout
    print(f"[+{elapsed:7.2f}s] {message}", file=stream, flush=True)
