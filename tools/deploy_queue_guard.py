#!/usr/bin/env python3
"""Deploy-queue guard: detect deploys stuck 'running' too long and (optionally)
remediate them through Dokploy's own API.

Runs as a sidecar in the alerting stack. Two halves, deliberately separated:

  * OBSERVE (always): query Dokploy deployment status, alert via the alert
    bridge when a deploy has been `running` past the ceiling. Read-only.
  * REMEDIATE (opt-in, DEPLOY_GUARD_REMEDIATE=1): kill the stuck build and clean
    the queue via `compose.killBuild` / `compose.cancelDeployment` /
    `compose.cleanQueues`, then re-check; if it is STILL running, escalate.

The remediation never touches Redis directly — Dokploy owns the BullMQ queue, so
we go through its API to keep queue + deployment records consistent. This
replaces the host watchdog's raw `LREM`/`DEL` surgery; the watchdog is now
observe-only.

Env:
  DOKPLOY_API_KEY / DOKPLOY_URL        Dokploy API (via libs.dokploy.get_dokploy)
  ALERT_BRIDGE_URL                     where to POST alerts (the feishu bridge)
  ALERTING_ENV_FILE                    env file to source (default /secrets/.env)
  DEPLOY_GUARD_CEILING_SECONDS         stuck threshold (default 1800 = 30 min)
  DEPLOY_GUARD_INTERVAL_SECONDS        loop interval (default 60)
  DEPLOY_GUARD_REMEDIATE               "1" to arm kill-on-timeout (default "0")
  DEPLOY_GUARD_GRACE_SECONDS           wait before post-kill re-check (default 20)
  DEPLOY_GUARD_RENOTIFY_SECONDS        re-alert suppression window (default 1800)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from libs.deploy_queue import (  # noqa: E402
    ComposeDeployments,
    build_deploy_guard_alert_payload,
    find_stuck_deploys,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("deploy-queue-guard")

DEFAULT_CEILING = 1800
DEFAULT_INTERVAL = 60
DEFAULT_GRACE = 20
DEFAULT_RENOTIFY = 1800


def _load_env_file(path: Path) -> None:
    """Source a KEY="value" env file into os.environ.

    EMPTY values are skipped so an env file rendered before Vault has populated a
    secret (e.g. DOKPLOY_API_KEY="") does not poison os.environ — a later
    non-empty render is then picked up on the next reload. Already-set non-empty
    keys are not overridden.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _make_client():
    from libs.dokploy import get_dokploy

    return get_dokploy()


def _list_composes(client):
    """Typed compose deployments with identity resolved at topology ingestion."""
    from libs.service_registry import service_id_for_dokploy

    out = []
    for project in client.list_projects():
        project_name = project.get("name", "")
        for env in project.get("environments", []):
            environment = (env.get("name") or "production").strip().lower()
            for compose in env.get("compose", []):
                compose_id = compose.get("composeId")
                if not compose_id:
                    continue
                name = compose.get("name") or compose.get("appName") or compose_id
                try:
                    deployments = client.get_compose_deployments(compose_id)
                except Exception as exc:  # one compose must not abort the sweep
                    logger.warning("deployments fetch failed for %s: %s", name, exc)
                    deployments = []
                service_id = service_id_for_dokploy(project_name, name) or ""
                if not service_id:
                    logger.warning(
                        "unregistered Dokploy compose identity: project=%s env=%s compose=%s",
                        project_name,
                        environment,
                        name,
                    )
                out.append(
                    ComposeDeployments(
                        compose_id=compose_id,
                        compose_name=name,
                        service_id=service_id,
                        environment=environment,
                        deployments=tuple(deployments),
                    )
                )
    return out


def _post_alert(payload: dict) -> None:
    bridge_url = os.environ.get("ALERT_BRIDGE_URL", "").strip()
    if not bridge_url:
        logger.warning("ALERT_BRIDGE_URL unset; alert not delivered: %s", payload)
        return
    from libs.infra_probes import post_alert_bridge_payload

    try:
        # The bridge enforces Basic Auth when BRIDGE_BASIC_AUTH_* is set; pass it
        # or these alerts get 401'd and silently dropped.
        post_alert_bridge_payload(
            bridge_url,
            payload,
            username=os.environ.get("BRIDGE_BASIC_AUTH_USERNAME", ""),
            password=os.environ.get("BRIDGE_BASIC_AUTH_PASSWORD", ""),
        )
    except Exception as exc:  # alerting is best-effort; never crash the loop
        logger.error("alert bridge delivery failed: %s", exc)


def _remediate(client, stuck, grace_seconds: int) -> None:
    """Kill + clean each stuck deploy via Dokploy API, then re-check and escalate."""
    for s in stuck:
        logger.warning(
            "REMEDIATE %s: killing stuck deploy %s (age=%ds)",
            s.compose_name,
            s.deployment_id,
            int(s.age_seconds),
        )
        _post_alert(
            build_deploy_guard_alert_payload(
                [s],
                action_note=f"killing stuck deploy {s.deployment_id} via Dokploy API",
            )
        )
        for label, call in (
            ("killBuild", client.kill_compose_build),
            ("cancelDeployment", client.cancel_compose_deployment),
            ("cleanQueues", client.clean_compose_queues),
        ):
            try:
                call(s.compose_id)
                logger.info("  %s ok for %s", label, s.compose_name)
            except Exception as exc:
                logger.error("  %s FAILED for %s: %s", label, s.compose_name, exc)

    if grace_seconds > 0:
        time.sleep(grace_seconds)

    # Re-check: anything still running past the ceiling means the API kill did
    # not clear it (e.g. the OOM-wedged build of dokploy#4461) -> escalate.
    still = find_stuck_deploys(
        _list_composes(client),
        time.time(),
        _env_int("DEPLOY_GUARD_CEILING_SECONDS", DEFAULT_CEILING),
    )
    killed = {s.compose_id for s in stuck}
    persistent = [s for s in still if s.compose_id in killed]
    if persistent:
        for s in persistent:
            logger.error(
                "ESCALATE %s: still running after kill (age=%ds) — needs manual/worker restart",
                s.compose_name,
                int(s.age_seconds),
            )
        _post_alert(
            build_deploy_guard_alert_payload(
                persistent,
                action_note="STILL running after Dokploy kill — manual intervention / worker restart required",
            )
        )


def run_once(
    client, *, ceiling: int, remediate: bool, grace: int, alerted: dict
) -> int:
    composes = _list_composes(client)
    stuck = find_stuck_deploys(composes, time.time(), ceiling)
    if not stuck:
        logger.info("deploy-queue guard: %d composes, none stuck", len(composes))
        return 0

    renotify = _env_int("DEPLOY_GUARD_RENOTIFY_SECONDS", DEFAULT_RENOTIFY)
    now = time.time()
    fresh = [s for s in stuck if now - alerted.get(s.compose_id, 0) >= renotify]
    for s in stuck:
        logger.warning(
            "STUCK %s: deploy %s running %ds (ceiling=%ds)",
            s.compose_name,
            s.deployment_id,
            int(s.age_seconds),
            ceiling,
        )
    if fresh:
        _post_alert(build_deploy_guard_alert_payload(fresh))
        for s in fresh:
            alerted[s.compose_id] = now

    if remediate:
        _remediate(client, stuck, grace)
    else:
        logger.info("remediation disabled (DEPLOY_GUARD_REMEDIATE!=1); observe-only")
    return len(stuck)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="run continuously")
    mode.add_argument(
        "--once", action="store_true", help="run a single sweep (default)"
    )
    args = parser.parse_args(argv)

    env_path = Path(os.environ.get("ALERTING_ENV_FILE", "/secrets/.env"))
    _load_env_file(env_path)
    ceiling = _env_int("DEPLOY_GUARD_CEILING_SECONDS", DEFAULT_CEILING)
    interval = _env_int("DEPLOY_GUARD_INTERVAL_SECONDS", DEFAULT_INTERVAL)
    grace = _env_int("DEPLOY_GUARD_GRACE_SECONDS", DEFAULT_GRACE)
    remediate = os.environ.get("DEPLOY_GUARD_REMEDIATE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    logger.info(
        "deploy-queue guard starting (ceiling=%ds remediate=%s interval=%ds)",
        ceiling,
        remediate,
        interval,
    )

    alerted: dict[str, float] = {}
    if not args.loop:
        try:
            client = _make_client()
        except Exception as exc:
            logger.error("Dokploy client unavailable: %s", exc)
            return 2
        return (
            1
            if run_once(
                client,
                ceiling=ceiling,
                remediate=remediate,
                grace=grace,
                alerted=alerted,
            )
            else 0
        )

    while True:
        try:
            # Reload secrets + rebuild the client each iteration (cheap) so the
            # sidecar idles rather than crashlooping, and PICKS UP a DOKPLOY_API_KEY
            # that Vault renders after the container started.
            _load_env_file(env_path)
            client = _make_client()
            run_once(
                client,
                ceiling=ceiling,
                remediate=remediate,
                grace=grace,
                alerted=alerted,
            )
        except Exception as exc:  # a bad sweep / missing key must not kill the sidecar
            logger.error("guard iteration failed: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
