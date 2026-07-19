"""Watcher-plugin surface for the single resident alerting sidecar (#543).

Operator decision (#543): ALL resident/continuous watching runs in ONE
low-footprint sidecar — the probe-runner process (`tools/infra_probe_runner.py
--loop`). The former `container-breakdown-watch` and `deploy-queue-guard`
compose sidecars are gone; their sweeps now run as REGISTERED WATCHER PLUGINS
invoked once per probe-loop iteration, each with its own long-lived in-memory
state (hysteresis streaks, renotify clocks) held on the plugin instance.

The plugin surface is deliberately tiny:

- :class:`ResidentWatcher` — base class. A watcher declares a ``name`` and an
  ``interval_seconds`` (mapped from its historical env knob for continuity,
  e.g. ``BREAKDOWN_INTERVAL_SECONDS``), and implements ``_sweep()``.
- :meth:`ResidentWatcher.maybe_run` — what the runner loop calls every
  iteration. It self-paces (a watcher whose own interval is longer than the
  loop cadence simply skips iterations until due) and NEVER raises: one broken
  watcher must not kill the probe loop or its sibling watchers.
- :func:`build_watchers` — the registry. Adding a new resident check means
  subclassing ResidentWatcher and registering it here — enforced ecosystem-side
  by tools/no_new_wheels_lint.py (its alert deliveries must map to a signal in
  docs/ssot/watchdog-signals.yaml).

Failure-domain note (#163/#475 monitor-the-monitor): because the watchers run
inside the probe-runner loop, a HUNG watcher stalls the loop, the runner's
state file goes stale, and the compose healthcheck (state-file freshness)
flips the container unhealthy -> Dokploy restarts it. A watcher that raises
(as opposed to hangs) is logged and retried next iteration. The former
standalone sidecars had NO healthcheck at all — a hung watcher previously
died invisibly.

Timing budget (60s default cadence, all sequential in one loop iteration):
~21 internal probes (5s timeout each, sub-second when healthy) + the public
route probes + one Docker-socket container sweep (10s client timeout; log
tails fetched only for broken containers) + one Dokploy deployments sweep
(one list_projects + one deployments fetch per compose, 30s client timeout).
Healthy-path total is a few seconds; the healthcheck tolerates transient
overruns (state file < 2min old, 3 retries), so only a sustained multi-minute
stall pages/restarts.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("resident-watchers")


class ResidentWatcher:
    """One resident watcher plugin: a named, self-paced, never-raising sweep."""

    name: str = "resident-watcher"
    interval_seconds: int = 60

    def __init__(self) -> None:
        self._last_run: float | None = None

    def _sweep(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def maybe_run(self, now: float | None = None) -> bool:
        """One loop tick: sweep if this watcher's own interval has elapsed.

        Returns True when a sweep ran (successfully or not). Never raises —
        a failing sweep is logged and retried on its next due tick, so one
        broken watcher cannot take down the probe loop or its siblings.
        """
        now = time.monotonic() if now is None else now
        if self._last_run is not None and now - self._last_run < self.interval_seconds:
            return False
        self._last_run = now
        try:
            self._sweep()
        except Exception as exc:  # noqa: BLE001 - watcher isolation is the contract
            logger.error("%s sweep failed: %s", self.name, exc)
        return True


def build_watchers(environ=None) -> list[ResidentWatcher]:
    """The registered resident watchers, configured from the environment.

    Imported lazily so the probe runner's stdlib-only probe path stays
    importable even where a watcher's dependencies (httpx for the Docker
    socket, libs.dokploy for the queue guard) are absent.
    """
    from libs.container_breakdown_watch import BreakdownWatch
    from libs.deploy_queue_guard import DeployQueueGuard

    return [BreakdownWatch(environ), DeployQueueGuard(environ)]
