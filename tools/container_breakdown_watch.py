#!/usr/bin/env python3
"""Container-breakdown watch: alert when a container is crash-looping / unhealthy,
*with the reason* pulled from its logs.

Sidecar in the alerting stack (mirrors tools/deploy_queue_guard.py). Read-only:
needs ``/var/run/docker.sock`` mounted ``:ro``. Talks to the Docker Engine API
over the socket with **httpx** (already a dependency) — no docker SDK.

Why it exists: the HTTP probes + cloudflare watchdog catch "service down"; the
deploy guard catches "deploy stuck". Neither says *why* a container is looping.
This turns hours of "down, unknown cause" into an immediate "down because Vault
creds missing" — the single signal that was absent during the finance_report
outage.

Env:
  DOCKER_SOCK                         docker socket path (default /var/run/docker.sock)
  ALERT_BRIDGE_URL                    where to POST alerts (the feishu bridge)
  ALERTING_ENV_FILE                   env file to source (default /secrets/.env)
  BREAKDOWN_INTERVAL_SECONDS          loop interval (default 60)
  BREAKDOWN_RENOTIFY_SECONDS          re-alert suppression window (default 1800)
  BREAKDOWN_FAILURE_THRESHOLD         consecutive broken polls before firing (default 3)
  BREAKDOWN_RECOVERY_THRESHOLD        consecutive healthy polls before RESOLVED (default 5)
  BREAKDOWN_LOG_TAIL                  log lines to scan per container (default 25)
  BRIDGE_BASIC_AUTH_USERNAME/PASSWORD optional basic-auth for the bridge

Flap hysteresis (#475): a container that blips broken/healthy/broken every poll
used to fire+resolve every single blip (333 firing + ~equal resolved in 48h during
the prefect/vault-agent incident, each RESOLVED wrongly resetting the renotify
timer). BREAKDOWN_FAILURE_THRESHOLD/BREAKDOWN_RECOVERY_THRESHOLD require N/M
CONSECUTIVE polls in a direction before firing/resolving -- see
libs.recency.evaluate_consecutive_hysteresis for the state machine (mirrors
tools/infra_probe_runner.py's proven _should_send pattern). A bad poll seen while
an incident is already active but before the recovery threshold is reached never
starts a new incident or resets the renotify clock.
"""
# alerts-as: container-breakdown-watch  (#542 no-new-wheels: registered T5 signal)

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from libs.container_breakdown import (  # noqa: E402
    Breakdown,
    build_breakdown_alert_payload,
    find_breakdown_containers,
)
from libs.recency import (  # noqa: E402
    ConsecutiveObservationState,
    evaluate_consecutive_hysteresis,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("container-breakdown-watch")
# httpx/httpcore log every 60s Docker-socket poll at INFO ("GET /containers/json 200"),
# which drowns the actual BREAKDOWN decisions in the container log (the reason a 07:33-style
# "what did it alert on?" needed a forensic dig). Keep only their warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DEFAULT_INTERVAL = 60
DEFAULT_RENOTIFY = 1800
DEFAULT_LOG_TAIL = 25
# 3 consecutive broken polls at the default 60s interval == ~3 minutes sustained
# before firing -- long enough that a single transient blip (e.g. a container
# briefly reporting "restarting" mid-deploy) never pages, short enough that a real
# crash-loop (which keeps re-entering Docker's restart backoff every few seconds)
# is caught well within one incident's first several minutes. Matches
# tools/infra_probe_runner.py's own DEFAULT_FAILURE_THRESHOLD at the same 60s
# cadence -- same reasoning, same codebase convention.
DEFAULT_FAILURE_THRESHOLD = 3
# 5 consecutive HEALTHY polls (~5 minutes) before RESOLVED. Deliberately more
# generous than the failure threshold: Docker's restart backoff can itself put a
# crash-looping container briefly into "running" between attempts, and the #475
# incident's 333 firing/resolved pairs were exactly this -- a single healthy-looking
# sample mistaken for real recovery. 5 minutes continuously healthy is well past any
# single backoff gap.
DEFAULT_RECOVERY_THRESHOLD = 5


def _load_env_file(path: Path) -> None:
    """Source a KEY="value" env file into os.environ (does not overwrite)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _docker_client(sock: str) -> httpx.Client:
    """Docker Engine API client over the unix socket (read-only usage)."""
    transport = httpx.HTTPTransport(uds=sock)
    return httpx.Client(transport=transport, base_url="http://docker", timeout=10.0)


def _list_containers(client: httpx.Client) -> list[dict]:
    resp = client.get("/containers/json", params={"all": "1"})
    resp.raise_for_status()
    return resp.json()


def _container_logs(client: httpx.Client, container_id: str, tail: int) -> str:
    """Recent stdout+stderr. The Engine multiplexes a frame header per line when
    the container has no TTY; substring matching still works on the decoded text,
    so we don't bother de-framing."""
    try:
        resp = client.get(
            f"/containers/{container_id}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(tail)},
        )
        resp.raise_for_status()
        return resp.content.decode("utf-8", errors="replace")
    except Exception as exc:  # logs are best-effort; never abort the sweep
        logger.warning("log fetch failed for %s: %s", container_id[:12], exc)
        return ""


def _post_alert(payload: dict) -> None:
    bridge_url = os.environ.get("ALERT_BRIDGE_URL", "").strip()
    if not bridge_url:
        logger.warning("ALERT_BRIDGE_URL unset; alert not delivered: %s", payload)
        return
    from libs.infra_probes import post_alert_bridge_payload

    try:
        post_alert_bridge_payload(
            bridge_url,
            payload,
            username=os.environ.get("BRIDGE_BASIC_AUTH_USERNAME", ""),
            password=os.environ.get("BRIDGE_BASIC_AUTH_PASSWORD", ""),
        )
    except Exception as exc:  # alerting is best-effort; never crash the loop
        logger.error("alert bridge delivery failed: %s", exc)


def sweep(client: httpx.Client, log_tail: int):
    containers = _list_containers(client)
    return find_breakdown_containers(
        containers, lambda cid: _container_logs(client, cid, log_tail)
    )


def run_once(
    client: httpx.Client,
    log_tail: int,
    container_state: dict,
    renotify: int,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    recovery_threshold: int = DEFAULT_RECOVERY_THRESHOLD,
) -> int:
    """One sweep. Flap hysteresis (#475): a container must be observed broken on
    ``failure_threshold`` CONSECUTIVE sweeps before it fires, and healthy on
    ``recovery_threshold`` CONSECUTIVE sweeps before it resolves -- a blip that
    flips broken/healthy every poll must not page every time. ``container_state``
    maps container name -> ``libs.recency.ConsecutiveObservationState`` and is
    mutated in place; the caller keeps one dict alive for the life of the poll
    loop (see main()). Returns the number of containers that fired this sweep
    (new incidents plus any still-active incident renotified this sweep).
    """
    now = time.monotonic()
    breakdowns = sweep(client, log_tail)
    broken_by_name = {b.container: b for b in breakdowns}

    fresh: list[Breakdown] = []
    for name, b in broken_by_name.items():
        state = container_state.setdefault(name, ConsecutiveObservationState())
        state.context = b  # latest breakdown, so a later RESOLVED reuses its labels
        action = evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=True,
            now=now,
            failure_threshold=failure_threshold,
            recovery_threshold=recovery_threshold,
            renotify_seconds=renotify,
        )
        if action == "fire":
            fresh.append(b)
        elif not state.active:
            logger.info(
                "BREAKDOWN-PENDING %s (%s): bad_streak=%d/%d — not yet firing: %s",
                name,
                b.state,
                state.bad_streak,
                failure_threshold,
                b.reason,
            )

    # Recovered = previously-tracked containers no longer broken THIS sweep, evaluated
    # against the recovery threshold. A container that flips back to broken before
    # reaching it stays part of the SAME active incident (evaluate_consecutive_hysteresis
    # neither resets bad_streak's incident nor the renotify clock in that case).
    recovered: list[Breakdown] = []
    for name, state in list(container_state.items()):
        if name in broken_by_name:
            continue
        action = evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=False,
            now=now,
            failure_threshold=failure_threshold,
            recovery_threshold=recovery_threshold,
            renotify_seconds=renotify,
        )
        if action == "resolve":
            # Resolve with the ORIGINAL breakdown (same state, hence same label set) so
            # the resolved alert matches the firing instance and the page actually
            # clears — a stub state like "recovered" forms a different label set and
            # never resolves the original.
            recovered.append(state.context)
        elif state.active:
            logger.info(
                "BREAKDOWN-RECOVERING %s: good_streak=%d/%d — not yet resolved",
                name,
                state.good_streak,
                recovery_threshold,
            )

    # Prune entries with no live signal left: either never crossed the failure
    # threshold and is healthy again, or just fully resolved above. Both leave
    # active=False, bad_streak=0, good_streak=0 -- the same shape a brand-new
    # entry starts in, so it's safe (and keeps memory bounded) to forget it; it is
    # recreated via setdefault() if the container breaks again later.
    for name in [
        n
        for n, s in container_state.items()
        if not s.active and s.bad_streak == 0 and s.good_streak == 0
    ]:
        container_state.pop(name, None)

    if fresh:
        for b in fresh:
            logger.warning("BREAKDOWN %s (%s): %s", b.container, b.state, b.reason)
        _post_alert(build_breakdown_alert_payload(fresh))
        logger.warning(
            "BREAKDOWN-ALERT firing count=%d -> posting to bridge: %s",
            len(fresh),
            ",".join(sorted(b.container for b in fresh)),
        )
    if recovered:
        logger.warning(
            "BREAKDOWN-RESOLVED count=%d -> posting to bridge: %s",
            len(recovered),
            ",".join(sorted(rec.container for rec in recovered)),
        )
        _post_alert(build_breakdown_alert_payload(recovered, firing=False))
    return len(fresh)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="run forever")
    args = parser.parse_args()

    _load_env_file(Path(os.environ.get("ALERTING_ENV_FILE", "/secrets/.env")))

    # breakdown-watch reads the WHOLE shared Docker engine (no per-env container
    # filter), so a per-env copy double-fires on the same container and mis-attributes
    # the env. Run it as a prod-only singleton: one watcher covers every container on
    # the box. Non-prod copies stay alive (so the service doesn't crash-loop) but never
    # sweep or alert.
    env_name = os.environ.get("ENV", "production")
    if env_name != "production":
        logger.info(
            "breakdown-watch is prod-only (one watcher sees the whole shared engine); "
            "skipping sweeps on env=%s",
            env_name,
        )
        if args.loop:
            while True:
                time.sleep(3600)
        return

    sock = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
    interval = int(os.environ.get("BREAKDOWN_INTERVAL_SECONDS", DEFAULT_INTERVAL))
    renotify = int(os.environ.get("BREAKDOWN_RENOTIFY_SECONDS", DEFAULT_RENOTIFY))
    failure_threshold = int(
        os.environ.get("BREAKDOWN_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD)
    )
    recovery_threshold = int(
        os.environ.get("BREAKDOWN_RECOVERY_THRESHOLD", DEFAULT_RECOVERY_THRESHOLD)
    )
    log_tail = int(os.environ.get("BREAKDOWN_LOG_TAIL", DEFAULT_LOG_TAIL))

    # container -> ConsecutiveObservationState (#475 flap hysteresis); one long-lived
    # in-memory dict for the life of this process. No disk state file: this watcher
    # runs as a single continuously-running `--loop` sidecar (restart: unless-stopped,
    # see platform/12.alerting/compose.yaml), never invoked repeatedly by cron/systemd,
    # so in-memory state already persists across every poll for as long as it matters.
    container_state: dict = {}
    client = _docker_client(sock)
    while True:
        try:
            run_once(
                client,
                log_tail,
                container_state,
                renotify,
                failure_threshold,
                recovery_threshold,
            )
        except Exception as exc:  # one bad sweep must not kill the watcher
            logger.error("sweep failed: %s", exc)
        if not args.loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
