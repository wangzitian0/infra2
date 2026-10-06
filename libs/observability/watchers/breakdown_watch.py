"""Container-breakdown watch: alert when a container is crash-looping / unhealthy,
*with the reason* pulled from its logs.

SSOT for ``libs.observability.watchers``'s breakdown watcher plugin.

Watcher plugin in the single resident alerting sidecar (#543) — runs inside
`tools/infra_probe_runner.py --loop` via `libs.observability.watchers.resident.build_watchers`
(it was a standalone compose sidecar before the merge). Read-only: needs
``/var/run/docker.sock`` mounted ``:ro`` on the probe-runner container. Talks
to the Docker Engine API over the socket with **httpx** (already a
dependency) — no docker SDK.

Why it exists: the HTTP probes + cloudflare watchdog catch "service down"; the
deploy-queue guard catches "deploy stuck". Neither says *why* a container is
looping. This turns hours of "down, unknown cause" into an immediate "down
because Vault creds missing" — the single signal that was absent during the
finance_report outage.

Env (unchanged across the #543 merge — the names map into per-watcher config):
  DOCKER_SOCK                         docker socket path (default /var/run/docker.sock)
  ALERT_BRIDGE_URL                    where to POST alerts (the feishu bridge)
  BREAKDOWN_INTERVAL_SECONDS          sweep interval (default 60; self-paced inside the loop)
  BREAKDOWN_RENOTIFY_SECONDS          re-alert window; 0 (default) = never on a timer
  BREAKDOWN_FAILURE_THRESHOLD         consecutive broken polls before firing (default 3)
  BREAKDOWN_RECOVERY_THRESHOLD        consecutive healthy polls before RESOLVED (default 5)
  BREAKDOWN_LOG_TAIL                  log lines to scan per container (default 25)
  BRIDGE_BASIC_AUTH_USERNAME/PASSWORD optional basic-auth for the bridge

Flap hysteresis (#475): a container that blips broken/healthy/broken every poll
used to fire+resolve every single blip (333 firing + ~equal resolved in 48h during
the prefect/vault-agent incident, each RESOLVED wrongly resetting the renotify
timer). BREAKDOWN_FAILURE_THRESHOLD/BREAKDOWN_RECOVERY_THRESHOLD require N/M
CONSECUTIVE polls in a direction before firing/resolving -- see
libs.observability.recency.evaluate_consecutive_hysteresis for the state machine (mirrors
tools/infra_probe_runner.py's proven _should_send pattern). A bad poll seen while
an incident is already active but before the recovery threshold is reached never
starts a new incident or resets the renotify clock.

Staging and previews never page (#903): only the production runner sweeps, and it
sees every container on the shared engine. A container whose environment is staging
or a preview slot (its canonical label, else inferred from its compose project and
name — ``container_identity``) is counted into the daily chronic digest instead of
paging, and its digest lines go out as a REPORT (``delivery=report``). Production
lines of the same digest still go to the pager: a floor-suppressed production
re-break is only ever told there.
"""
# alerts-as: container-breakdown-watch  (#542 no-new-wheels: registered T5 signal)

from __future__ import annotations

import logging
import os

import httpx

from libs.alerting import is_report_only_environment, mark_report_payload
from libs.observability.breakdown import (
    Breakdown,
    build_breakdown_alert_payload,
    find_breakdown_containers,
)
from libs.observability.recency import (
    ConsecutiveObservationState,
    evaluate_consecutive_hysteresis,
)
from libs.observability.watchers.resident import ResidentWatcher

logger = logging.getLogger("container-breakdown-watch")
# httpx/httpcore log every 60s Docker-socket poll at INFO ("GET /containers/json 200"),
# which drowns the actual BREAKDOWN decisions in the container log (the reason a 07:33-style
# "what did it alert on?" needed a forensic dig). Keep only their warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

DEFAULT_INTERVAL = 60
# 0 = an unchanged ongoing incident is never re-paged on a timer; escalation and the
# chronic digest carry it (#475). The compose default has been 0 since #475; this module
# default now says the same, so the registry entry can state it (#903).
DEFAULT_RENOTIFY = 0
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
# Stay-resolved floor (#658 recommendation 7, closes the class behind #475): once an
# incident has RESOLVED, the same container does not page again for this long. A
# container that keeps crossing the failure threshold inside the floor is "chronic":
# it is counted, logged, and reported ONCE per digest window instead of re-firing
# (2026-09-08: platform-prefect-worker fired 05:48 → resolved 05:55 → fired again 06:45,
# every ~2 h, for seven weeks).
DEFAULT_STAY_RESOLVED_SECONDS = 6 * 3600
DEFAULT_CHRONIC_DIGEST_SECONDS = 24 * 3600


def is_report_only(breakdown: Breakdown | None) -> bool:
    """True for a staging or preview container: digest only, never the pager (#903).

    Keyed on the container's environment as ``container_identity`` resolved it — the
    canonical ``party.zitian.infra.environment`` label, or, only when that is absent,
    what its compose project and name say. No breakdown at hand, or any environment
    not on the report-only allowlist, pages.
    """
    return breakdown is not None and is_report_only_environment(breakdown.environment)


def _docker_client(sock: str) -> httpx.Client:
    """Docker Engine API client over the unix socket (read-only usage)."""
    transport = httpx.HTTPTransport(uds=sock)
    return httpx.Client(transport=transport, base_url="http://docker", timeout=10.0)


def _list_containers(client: httpx.Client) -> list[dict]:
    resp = client.get("/containers/json", params={"all": "1"})
    resp.raise_for_status()
    return resp.json()


def _container_logs(client: httpx.Client, container_id: str, tail: int) -> str:
    """Recent stdout+stderr, de-framed (``demux_docker_logs``)."""
    try:
        resp = client.get(
            f"/containers/{container_id}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(tail)},
        )
        resp.raise_for_status()
        return demux_docker_logs(resp.content)
    except Exception as exc:  # logs are best-effort; never abort the sweep
        logger.warning("log fetch failed for %s: %s", container_id[:12], exc)
        return ""


def demux_docker_logs(raw: bytes) -> str:
    """The text of a Docker Engine log stream.

    Without a TTY the Engine prefixes every frame with an 8-byte header (stream type,
    three zero bytes, big-endian length). Substring matching survived it, but the log
    tail a card shows (#905) must not start every line with header bytes. A stream
    that does not start with a header (a TTY container) is returned as it is.
    """
    frames: list[bytes] = []
    position = 0
    while (
        position + 8 <= len(raw)
        and raw[position] in (0, 1, 2)
        and raw[position + 1 : position + 4] == b"\x00\x00\x00"
    ):
        size = int.from_bytes(raw[position + 4 : position + 8], "big")
        frames.append(raw[position + 8 : position + 8 + size])
        position += 8 + size
    if not frames:
        return raw.decode("utf-8", errors="replace")
    frames.append(raw[position:])
    return b"".join(frames).decode("utf-8", errors="replace")


def _post_alert(payload: dict) -> None:
    bridge_url = os.environ.get("ALERT_BRIDGE_URL", "").strip()
    if not bridge_url:
        logger.warning("ALERT_BRIDGE_URL unset; alert not delivered: %s", payload)
        return
    from libs.observability.probes import post_alert_bridge_payload

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


def _evaluate_broken_container(
    name: str,
    b: Breakdown,
    container_state: dict,
    resolved_at: dict,
    chronic: dict,
    chronic_context: dict,
    now: float,
    wall: float,
    failure_threshold: int,
    recovery_threshold: int,
    renotify: int,
    stay_resolved_seconds: int,
) -> Breakdown | None:
    """Evaluate one broken container against flap hysteresis and chronic floor."""
    import dataclasses

    state = container_state.setdefault(name, ConsecutiveObservationState())
    previous = state.context
    b = dataclasses.replace(
        b, since=previous.since if previous is not None and previous.since else wall
    )
    state.context = b
    escalated = bool(
        state.active
        and previous is not None
        and (previous.state, previous.reason) != (b.state, b.reason)
    )
    action = evaluate_consecutive_hysteresis(
        state=state,
        is_bad_now=True,
        now=now,
        failure_threshold=failure_threshold,
        recovery_threshold=recovery_threshold,
        renotify_seconds=renotify,
    )
    if action == "none" and state.active:
        if escalated:
            logger.warning(
                "BREAKDOWN-ESCALATED %s: %s/%s -> %s/%s",
                name,
                previous.state,
                previous.reason,
                b.state,
                b.reason,
            )
            action = "fire"
            state.last_alert_at = now
        elif renotify <= 0:
            chronic[name] = int(chronic.get(name, 0)) + 1
            chronic_context[name] = b

    if action == "fire":
        since_resolved = now - resolved_at.get(name, float("-inf"))
        if is_report_only(b):
            chronic[name] = int(chronic.get(name, 0)) + 1
            chronic_context[name] = b
            logger.info(
                "BREAKDOWN-REPORT-ONLY %s (%s): staging/preview, digest only — %s",
                name,
                b.state,
                b.reason,
            )
            return None
        if (
            since_resolved < stay_resolved_seconds
            and state.bad_streak == failure_threshold
        ):
            chronic[name] = int(chronic.get(name, 0)) + 1
            chronic_context[name] = b
            logger.warning(
                "BREAKDOWN-CHRONIC %s (%s): re-broke %ds after resolving (floor %ds); "
                "suppressed page #%d — %s",
                name,
                b.state,
                int(since_resolved),
                stay_resolved_seconds,
                chronic[name],
                b.reason,
            )
            return None
        chronic.pop(name, None)
        chronic_context.pop(name, None)
        return b

    if not state.active:
        logger.info(
            "BREAKDOWN-PENDING %s (%s): bad_streak=%d/%d — not yet firing: %s",
            name,
            b.state,
            state.bad_streak,
            failure_threshold,
            b.reason,
        )
    return None


def _process_recovered_containers(
    broken_by_name: dict[str, Breakdown],
    container_state: dict,
    resolved_at: dict,
    now: float,
    failure_threshold: int,
    recovery_threshold: int,
    renotify: int,
) -> list[Breakdown]:
    """Identify and record containers that have consecutively recovered."""
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
            resolved_at[name] = now
            if not is_report_only(state.context):
                recovered.append(state.context)
        elif state.active:
            logger.info(
                "BREAKDOWN-RECOVERING %s: good_streak=%d/%d — not yet resolved",
                name,
                state.good_streak,
                recovery_threshold,
            )
    return recovered


def _emit_chronic_digest(
    chronic: dict,
    chronic_context: dict,
    container_state: dict,
    resolved_at: dict,
    now: float,
    chronic_digest_seconds: int,
    stay_resolved_seconds: int,
) -> None:
    """Post chronic incident digest periodically."""
    chronic_names = sorted(n for n in chronic if n != "__digest_at__")
    if chronic_names and "__digest_at__" not in chronic:
        chronic["__digest_at__"] = now
    if not (
        chronic_names
        and now - chronic.get("__digest_at__", float("-inf")) >= chronic_digest_seconds
    ):
        return

    def context(name: str) -> Breakdown | None:
        tracked = container_state.get(name)
        return chronic_context.get(name) or (tracked.context if tracked else None)

    digest = [
        Breakdown(
            container=name,
            state="chronic",
            reason=(
                f"staging/预览容器,从不呼人:{chronic[name]} 次巡检坏着"
                if is_report_only(context(name))
                else f"恢复后 {stay_resolved_seconds // 3600}h 保持期内又坏了 "
                f"{chronic[name]} 次"
                if now - resolved_at.get(name, float("-inf")) < stay_resolved_seconds
                else f"仍坏着:上次呼人后已 {chronic[name]} 次巡检"
            ),
            detail=context(name).detail if context(name) else "",
            log_tail=context(name).log_tail if context(name) else "",
            since=context(name).since if context(name) else 0.0,
            service_id=context(name).service_id if context(name) else "",
            component=context(name).component if context(name) else "container",
            environment=(context(name).environment if context(name) else "production"),
        )
        for name in chronic_names
    ]
    logger.warning(
        "BREAKDOWN-CHRONIC-DIGEST count=%d -> posting once per %dh: %s",
        len(digest),
        chronic_digest_seconds // 3600,
        ",".join(chronic_names),
    )
    paging = [line for line in digest if not is_report_only(line)]
    reports = [line for line in digest if is_report_only(line)]
    if paging:
        _post_alert(
            build_breakdown_alert_payload(
                paging, severity="warning", alertname="ContainerBreakdownChronic"
            )
        )
    if reports:
        _post_alert(
            mark_report_payload(
                build_breakdown_alert_payload(
                    reports,
                    severity="warning",
                    alertname="ContainerBreakdownChronic",
                )
            )
        )
    chronic["__digest_at__"] = now
    for name in chronic_names:
        chronic.pop(name, None)
        chronic_context.pop(name, None)


def run_once(
    client: httpx.Client,
    log_tail: int,
    container_state: dict,
    renotify: int,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    recovery_threshold: int = DEFAULT_RECOVERY_THRESHOLD,
    *,
    resolved_at: dict | None = None,
    chronic: dict | None = None,
    chronic_context: dict | None = None,
    stay_resolved_seconds: int = DEFAULT_STAY_RESOLVED_SECONDS,
    chronic_digest_seconds: int = DEFAULT_CHRONIC_DIGEST_SECONDS,
) -> int:
    """One sweep over all running containers evaluating flap hysteresis."""
    import time

    now = time.monotonic()
    wall = time.time()
    resolved_at = {} if resolved_at is None else resolved_at
    chronic = {} if chronic is None else chronic
    chronic_context = {} if chronic_context is None else chronic_context

    breakdowns = sweep(client, log_tail)
    broken_by_name = {b.container: b for b in breakdowns}

    fresh: list[Breakdown] = []
    for name, b in broken_by_name.items():
        firing = _evaluate_broken_container(
            name=name,
            b=b,
            container_state=container_state,
            resolved_at=resolved_at,
            chronic=chronic,
            chronic_context=chronic_context,
            now=now,
            wall=wall,
            failure_threshold=failure_threshold,
            recovery_threshold=recovery_threshold,
            renotify=renotify,
            stay_resolved_seconds=stay_resolved_seconds,
        )
        if firing is not None:
            fresh.append(firing)

    recovered = _process_recovered_containers(
        broken_by_name=broken_by_name,
        container_state=container_state,
        resolved_at=resolved_at,
        now=now,
        failure_threshold=failure_threshold,
        recovery_threshold=recovery_threshold,
        renotify=renotify,
    )

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
        _post_alert(build_breakdown_alert_payload(recovered, firing=False, now=wall))

    _emit_chronic_digest(
        chronic=chronic,
        chronic_context=chronic_context,
        container_state=container_state,
        resolved_at=resolved_at,
        now=now,
        chronic_digest_seconds=chronic_digest_seconds,
        stay_resolved_seconds=stay_resolved_seconds,
    )

    return len(fresh)


class BreakdownWatch(ResidentWatcher):
    """The breakdown sweep as a resident watcher plugin (#543).

    breakdown-watch reads the WHOLE shared Docker engine (no per-env container
    filter), so a per-env copy double-fires on the same container and
    mis-attributes the env. It stays a prod-only singleton: only the production
    runner's plugin sweeps; non-prod copies are registered but no-op (logged
    once) — the exact gating the standalone sidecar had.
    """

    name = "container-breakdown-watch"

    def __init__(self, environ=None) -> None:
        super().__init__()
        env = os.environ if environ is None else environ
        self.interval_seconds = int(
            env.get("BREAKDOWN_INTERVAL_SECONDS", DEFAULT_INTERVAL)
        )
        self.renotify = int(env.get("BREAKDOWN_RENOTIFY_SECONDS", DEFAULT_RENOTIFY))
        self.failure_threshold = int(
            env.get("BREAKDOWN_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD)
        )
        self.recovery_threshold = int(
            env.get("BREAKDOWN_RECOVERY_THRESHOLD", DEFAULT_RECOVERY_THRESHOLD)
        )
        self.log_tail = int(env.get("BREAKDOWN_LOG_TAIL", DEFAULT_LOG_TAIL))
        self.sock = env.get("DOCKER_SOCK", "/var/run/docker.sock")
        # Idle only on a recognised non-production runner: `prod` or a typo sweeps.
        self.enabled = not is_report_only_environment(env.get("ENV"))
        if not self.enabled:
            logger.info(
                "breakdown-watch is prod-only (one watcher sees the whole shared "
                "engine); plugin registered but idle on env=%s",
                env.get("ENV"),
            )
        # container -> ConsecutiveObservationState (#475 flap hysteresis); one
        # long-lived in-memory dict for the life of the sidecar process. No disk
        # state file: the plugin lives inside the continuously-running probe
        # runner (restart: unless-stopped), never re-invoked by cron/systemd, so
        # in-memory state persists across every poll for as long as it matters.
        self.container_state: dict = {}
        # #658 stay-resolved floor + chronic digest; same lifetime as container_state.
        self.resolved_at: dict = {}
        self.chronic: dict = {}
        self.chronic_context: dict = {}
        self.stay_resolved_seconds = int(
            env.get("BREAKDOWN_STAY_RESOLVED_SECONDS", DEFAULT_STAY_RESOLVED_SECONDS)
        )
        self.chronic_digest_seconds = int(
            env.get("BREAKDOWN_CHRONIC_DIGEST_SECONDS", DEFAULT_CHRONIC_DIGEST_SECONDS)
        )
        self._client: httpx.Client | None = None

    def _sweep(self) -> None:
        if not self.enabled:
            return
        if self._client is None:
            self._client = _docker_client(self.sock)
        run_once(
            self._client,
            self.log_tail,
            self.container_state,
            self.renotify,
            self.failure_threshold,
            self.recovery_threshold,
            resolved_at=self.resolved_at,
            chronic=self.chronic,
            chronic_context=self.chronic_context,
            stay_resolved_seconds=self.stay_resolved_seconds,
            chronic_digest_seconds=self.chronic_digest_seconds,
        )


__all__ = [
    "BreakdownWatch",
    "is_report_only",
    "DEFAULT_CHRONIC_DIGEST_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_INTERVAL",
    "DEFAULT_LOG_TAIL",
    "DEFAULT_RECOVERY_THRESHOLD",
    "DEFAULT_RENOTIFY",
    "DEFAULT_STAY_RESOLVED_SECONDS",
    "logger",
    "run_once",
    "sweep",
]
