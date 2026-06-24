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
  BREAKDOWN_LOG_TAIL                  log lines to scan per container (default 25)
  BRIDGE_BASIC_AUTH_USERNAME/PASSWORD optional basic-auth for the bridge
"""

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
    build_breakdown_alert_payload,
    find_breakdown_containers,
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
    client: httpx.Client, log_tail: int, last_alerted: dict, renotify: int
) -> int:
    """One sweep. Alerts only on breakdowns outside the renotify window. Returns
    the number of containers alerted on."""
    now = time.monotonic()
    breakdowns = sweep(client, log_tail)
    # last_alerted maps container -> (last_alert_monotonic, the breakdown we alerted on)
    fresh = [
        b
        for b in breakdowns
        if now - last_alerted.get(b.container, (-renotify - 1, None))[0] >= renotify
    ]
    if fresh:
        for b in fresh:
            logger.warning("BREAKDOWN %s (%s): %s", b.container, b.state, b.reason)
            last_alerted[b.container] = (now, b)
        _post_alert(build_breakdown_alert_payload(fresh))
        logger.warning(
            "BREAKDOWN-ALERT firing count=%d -> bridge: %s",
            len(fresh),
            ",".join(sorted(b.container for b in fresh)),
        )
    # Recovered = previously-alerted containers no longer broken. Resolve them with the
    # ORIGINAL breakdown (same state, hence same label set) so the resolved alert
    # matches the firing instance and the page actually clears — a stub state like
    # "recovered" forms a different label set and never resolves the original. Then
    # forget them so they can re-alert if they break again.
    live = {b.container for b in breakdowns}
    recovered = [rec for name, (_, rec) in last_alerted.items() if name not in live]
    for name in [n for n in last_alerted if n not in live]:
        last_alerted.pop(name, None)
    if recovered:
        logger.warning(
            "BREAKDOWN-RESOLVED count=%d -> bridge: %s",
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
    log_tail = int(os.environ.get("BREAKDOWN_LOG_TAIL", DEFAULT_LOG_TAIL))

    # container -> (last_alert_monotonic, breakdown we alerted on, kept so the
    # resolved alert can reuse the original state/labels)
    last_alerted: dict = {}
    client = _docker_client(sock)
    while True:
        try:
            run_once(client, log_tail, last_alerted, renotify)
        except Exception as exc:  # one bad sweep must not kill the watcher
            logger.error("sweep failed: %s", exc)
        if not args.loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
