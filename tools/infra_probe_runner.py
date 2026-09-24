#!/usr/bin/env python3
"""The single resident alerting sidecar (#543): infra probes + watcher plugins.

One process, one container (`platform-alerting-probes${ENV_SUFFIX}`), all
resident/continuous watching:

  1. the probe loop (INFRA_PROBE_SPECS / PUBLIC_ROUTE_PROBE_SPECS), alerting
     through the internal bridge;
  2. registered watcher plugins (libs/resident_watchers.py) — the container
     breakdown watch and the deploy-queue guard, formerly two separate compose
     sidecars — invoked once per loop iteration with their own per-watcher
     state and self-paced intervals.

The compose healthcheck (state-file freshness) covers the WHOLE loop: a hung
probe cycle OR a hung watcher stalls the next state write, the file goes
stale, and the container flips unhealthy -> Dokploy restarts it (#163/#475
monitor-the-monitor; the standalone sidecars had no healthcheck at all).
See libs/resident_watchers.py for the plugin surface + timing budget.
"""
# alert-delivery-exempt: the probe ENGINE — delivers on behalf of every registered internal probe signal (per-spec, not per-module)

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import logging
import os
import traceback
import time
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from libs.alerting import is_report_only_environment, mark_report_payload
from libs.infra_probes import (
    HTTP_PROBE_HEADERS,
    build_probe_alert_payload,
    failed_results,
    group_severity,
    is_misconfigured,
    parse_probe_specs,
    post_alert_bridge_payload,
    run_probes,
)
from libs.observability.probes import failure_domain
from libs.observability.local_ledger import record_probe_round


DEFAULT_PROBE_SPECS = """
alert-bridge-http|http|http://platform-alerting:8080/health|200|critical|5
"""
DEFAULT_PROBE_INTERVAL_SECONDS = 60
# 0 = never re-page an unchanged ongoing incident on a timer (#903). A stream re-sends
# only when the identity of its failing set changes (probe, kind, failure domain) —
# which is also how an escalation reaches the pager — and the once-a-day chronic digest
# carries the rest. 1800 re-sent one route failure 870 times in 16 days (#901).
DEFAULT_RENOTIFY_SECONDS = 0
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RECOVERY_THRESHOLD = 2
DEFAULT_STATE_FILE = "/tmp/infra_probe_runner_state.json"
# The `<group>:misconfigured` lane: a distinct, warning-severity alert for `command`
# probes whose failure is not (yet) evidence about their target —
#   - a probe that reported its own configuration missing (exit EX_CONFIG), for good;
#   - a probe that has never passed since the runner started, for a grace period only.
MISCONFIGURED_ALERT_NAME = "InfraProbeMisconfigured"
MISCONFIGURED_SEVERITY = "warning"
# The grace period (#726). A round-trip that never passed is as likely a broken target as
# a broken probe: a runner recreated in the middle of the OpenPanel NOSCRIPT outage held
# `openpanel-roundtrip` in this lane as "misconfigured" while every /track failed. After
# this many consecutive failed runs, spanning at least this long (three round-trip
# intervals of OBS_ROUNDTRIP_INTERVAL_SECONDS=300), it fails in the group's normal stream
# at its declared severity. A failing round-trip re-runs on every loop, so the run count
# alone would escalate after three minutes.
DEFAULT_NEVER_GREEN_ESCALATION_FAILURES = 3
DEFAULT_NEVER_GREEN_ESCALATION_SECONDS = 900
# The chronic digest (#903): once per UTC day, a REPORT (delivery=report, never the
# pager) listing every stream that has been failing for longer than a day — the probe
# runner's counterpart of the breakdown watcher's ContainerBreakdownChronic.
CHRONIC_DIGEST_ALERT_NAME = "InfraProbeChronic"
CHRONIC_AFTER_SECONDS = 24 * 3600
# Heartbeat contract v2 (#903): `ok` is the probe LOOP's health (a group run raised,
# the state save failed, or a send still pending failed its bridge delivery) — never a
# probe verdict. `failing_public_routes` lists only the routes this runner has already
# told someone about (see _paged_public_routes): the Worker stands down on exactly those.
HEARTBEAT_SCHEMA = 2
PUBLIC_ROUTE_GROUP = "public-route"


def _log_send(stream_key: str, results: list, severity_override: str | None) -> None:
    """Structured line on every real send, so the alerting loop is observable (it was a
    black box — failures-only, nothing on a successful send)."""
    failures = failed_results(results)
    status = "firing" if failures else "resolved"
    severity = severity_override or group_severity(failures)
    names = ",".join(sorted(r.spec.name for r in failures)) or "-"
    print(
        f"probe-runner send stream={stream_key} status={status} "
        f"severity={severity} failures={len(failures)} probes={names}",
        flush=True,
    )


def _cascades_to_failing_root(
    name: str, dep_of: dict[str, str], failed_names: set[str]
) -> bool:
    """True if `name` is a cascade symptom to suppress: its `depends_on` chain leads to a
    failing ROOT. False — i.e. keep alerting — when the immediate dependency is healthy (a
    real independent failure) OR the chain forms a cycle (no root → fail closed). A probe
    with no `depends_on` is itself a root and is never suppressed.
    """
    cur = dep_of.get(name)
    if not cur:
        return False  # no dependency -> this IS a root, always page
    seen = {name}
    while cur:
        if cur not in failed_names:
            return False  # dependency healthy -> independent failure, do not suppress
        if cur in seen:
            return False  # cycle -> no real root -> fail closed (alert)
        seen.add(cur)
        cur = dep_of.get(cur)
    return True  # reached a failing node with no failing dependency = the root


def _resolve_failing_root(
    name: str, dep_of: dict[str, str], failed_names: set[str]
) -> str:
    """The deepest failing node `name`'s depends_on chain reaches — the actual root being
    paged. Only meaningful for a cascade symptom (caller guarantees a root exists). For
    A->B->C this returns C, not the immediate dependency B."""
    seen = {name}
    root = name
    cur = dep_of.get(name)
    while cur and cur in failed_names and cur not in seen:
        root = cur
        seen.add(cur)
        cur = dep_of.get(cur)
    return root


class ProbeGroup(NamedTuple):
    name: str
    raw_specs: str
    alert_name: str
    external_url: str


def _configure_logging() -> None:
    """Give the process a root handler, and let the two watchers speak at INFO.

    Without a handler, `logging`'s handler of last resort emits WARNING and above only —
    so every `logger.info` in the watchers was silently dropped, and the only thing they
    could say was a warning. That is why the deploy-queue guard's one line per sweep was
    a per-compose WARNING (32,236 lines in 24 h on production) and why its replacement,
    a single INFO summary, appeared nowhere at all when v1.1.78 reached staging.

    The root stays at WARNING so nothing else in the process becomes chatty; the two
    watcher loggers are raised to INFO explicitly. httpx/httpcore are pinned to WARNING
    in libs.container_breakdown_watch, which polls the Docker socket every minute.
    """
    # force=True: basicConfig is a no-op once the root logger has any handler, so
    # without it a library that configured logging first would leave the root level and
    # handler as it wanted them — and the summaries back where they started.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for name in ("deploy-queue-guard", "container-breakdown-watch"):
        logging.getLogger(name).setLevel(logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _configure_logging()
    env_file = Path(os.getenv("ALERTING_ENV_FILE", "/secrets/.env"))
    _load_env_file(env_file)
    interval = int(
        os.getenv("INFRA_PROBE_INTERVAL_SECONDS", str(DEFAULT_PROBE_INTERVAL_SECONDS))
    )
    renotify_seconds = int(
        os.getenv("INFRA_PROBE_RENOTIFY_SECONDS", str(DEFAULT_RENOTIFY_SECONDS))
    )
    failure_threshold = int(
        os.getenv("INFRA_PROBE_FAILURE_THRESHOLD", str(DEFAULT_FAILURE_THRESHOLD))
    )
    recovery_threshold = int(
        os.getenv("INFRA_PROBE_RECOVERY_THRESHOLD", str(DEFAULT_RECOVERY_THRESHOLD))
    )
    state_path = Path(os.getenv("INFRA_PROBE_STATE_FILE", DEFAULT_STATE_FILE))
    # Mirror run_once's gate: dry-run must not emit heartbeat/state (no misleading
    # liveness signal during local/debug runs).
    dry_run = os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1"

    # Resident watcher plugins (#543): breakdown watch + deploy-queue guard run
    # inside THIS loop. Dry-run skips them entirely — they post real alerts to
    # the bridge, which a local/debug run must never do.
    watchers = [] if dry_run else _build_watchers()

    while True:
        # Liveness-first heartbeat (#369): prove the runner is alive at the START of
        # every iteration, BEFORE running probes. Crash/OOM/hang during a probe cycle
        # then surfaces as heartbeat staleness within one interval — independent of how
        # long the probe cycle takes. The post-probe heartbeat below still carries the
        # ok/failure status; this ping is flagged `liveness` so the watchdog keeps that
        # verdict instead of flipping it back to ok — the flip is a status change, and
        # while any probe failed every flip was a KV write (1198 puts/day, 2026-09-15).
        if args.loop and not dry_run:
            _post_heartbeat(
                ok=True,
                detail="probe loop iteration starting",
                liveness=True,
                state=_load_state(state_path),
            )
            # Local liveness-first mirror of the heartbeat: refresh the state
            # file BEFORE the (possibly slow) probe+watcher work, so the
            # compose healthcheck's freshness window measures loop liveness,
            # not iteration duration — a long all-timeouts probe cycle plus
            # watcher sweeps must not read as a hung loop.
            _touch_state(state_path)
        _load_env_file(env_file)
        try:
            exit_code = run_once(
                as_json=args.json,
                renotify_seconds=renotify_seconds,
                failure_threshold=failure_threshold,
                recovery_threshold=recovery_threshold,
                state_path=state_path,
            )
        except Exception as exc:  # noqa: BLE001 - looped probes must keep running.
            print(f"infra probe iteration failed: {exc}", flush=True)
            traceback.print_exc()
            if not dry_run:
                _post_heartbeat(
                    ok=False,
                    detail=f"probe loop raised {type(exc).__name__}: {exc}",
                    state=_load_state(state_path),
                )
            exit_code = 1
        # Watcher plugins run AFTER the probes in the same iteration; each
        # maybe_run self-paces on its own interval and never raises (one broken
        # watcher must not kill the loop or its siblings). A HUNG watcher
        # stalls the loop -> stale state file -> unhealthy -> restart.
        for watcher in watchers:
            watcher.maybe_run()
        if not args.loop:
            return exit_code
        time.sleep(interval)


def _build_watchers() -> list:
    """Construct the registered resident watcher plugins (#543).

    A module-level seam so tests can stub the watcher set; the real registry
    lives in libs/resident_watchers.build_watchers."""
    from libs.resident_watchers import build_watchers

    return build_watchers()


def _touch_state(state_path: Path) -> None:
    """Refresh the healthcheck state file's mtime (liveness-first, #543)."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.touch()
    except OSError as exc:
        print(f"infra probe state touch failed: {exc}", flush=True)


def _deploy_env() -> str:
    """Resolve the deploy environment with the same precedence the heartbeat uses
    (the runner sets INFRA_PROBE_HEARTBEAT_ENV, not always ENV), normalized."""
    return (
        (
            os.getenv("INFRA_PROBE_HEARTBEAT_ENV")
            or os.getenv("ENV")
            or os.getenv("DEPLOY_ENV")
            or "production"
        )
        .strip()
        .lower()
    )


def _host_specs_for_env(specs: list) -> list:
    """Host `resource` probes describe the single shared host, so only the
    production runner should run them — otherwise the prod and staging runners
    (co-located on that host) both alert on the same CPU/mem/disk threshold.
    Only a recognised non-production runner skips them (#903 review): `prod` or a
    typo still watches the host rather than leaving it unwatched.
    """
    if not is_report_only_environment(_deploy_env()):
        return specs
    return [spec for spec in specs if spec.kind != "resource"]


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _track_never_green(
    results: list, ever_succeeded: set[str], never_green: dict, now: float
) -> None:
    """Count consecutive failed runs of each `command` probe that has never passed.

    Runs before cascade suppression: a round-trip muted because its root is down still
    failed, and once the root recovers its streak is real history, not a fresh start.
    A run that reported its own configuration missing tested nothing, so it ends the
    streak instead of extending it.
    """
    for result in results:
        if result.spec.kind != "command":
            continue
        name = result.spec.name
        if result.ok or name in ever_succeeded or is_misconfigured(result):
            never_green.pop(name, None)
            continue
        streak = never_green.setdefault(name, {"since": now, "runs": 0})
        streak["runs"] = int(streak.get("runs") or 0) + 1


def _escalated(streak: dict | None, now: float, runs: int, seconds: int) -> bool:
    if not streak:
        return False
    failing_for = now - float(streak.get("since", now))
    return int(streak.get("runs") or 0) >= max(1, runs) and failing_for >= max(
        0, seconds
    )


def _grace_note(result, alert_name: str, runs: int, seconds: int):
    """A never-passed failure in the misconfigured lane, worded as what it is.

    The text is fixed per probe (no counters), so the card reads the same on every run.
    It no longer feeds the stream's fingerprint (#903: identity only), so wording it
    differently could not restart the debounce either.
    """
    return dataclasses.replace(
        result,
        summary=(
            "has not passed since the probe runner started — probe or target broken; "
            f"becomes {alert_name} after {runs} failed runs over {seconds // 60} min: "
            f"{result.summary}"
        ),
    )


def run_once(
    *,
    as_json: bool = False,
    renotify_seconds: int | None = None,
    failure_threshold: int | None = None,
    recovery_threshold: int | None = None,
    state_path: Path | None = None,
    escalation_failures: int | None = None,
    escalation_seconds: int | None = None,
) -> int:
    groups = _probe_groups()
    state_path = state_path or Path(
        os.getenv("INFRA_PROBE_STATE_FILE", DEFAULT_STATE_FILE)
    )
    renotify_seconds = (
        renotify_seconds
        if renotify_seconds is not None
        else int(
            os.getenv("INFRA_PROBE_RENOTIFY_SECONDS", str(DEFAULT_RENOTIFY_SECONDS))
        )
    )
    failure_threshold = (
        failure_threshold
        if failure_threshold is not None
        else int(
            os.getenv("INFRA_PROBE_FAILURE_THRESHOLD", str(DEFAULT_FAILURE_THRESHOLD))
        )
    )
    recovery_threshold = (
        recovery_threshold
        if recovery_threshold is not None
        else int(
            os.getenv("INFRA_PROBE_RECOVERY_THRESHOLD", str(DEFAULT_RECOVERY_THRESHOLD))
        )
    )
    if escalation_failures is None:
        escalation_failures = _env_int(
            "INFRA_PROBE_NEVER_GREEN_ESCALATION_FAILURES",
            DEFAULT_NEVER_GREEN_ESCALATION_FAILURES,
        )
    if escalation_seconds is None:
        escalation_seconds = _env_int(
            "INFRA_PROBE_NEVER_GREEN_ESCALATION_SECONDS",
            DEFAULT_NEVER_GREEN_ESCALATION_SECONDS,
        )
    state = _load_state(state_path)
    now = time.time()
    any_failures = False
    json_results: dict[str, list[dict]] = {}
    dry_run = os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1"
    ever_succeeded = set(state.get("ever_succeeded", []))
    if not isinstance(state.get("never_green"), dict):
        state["never_green"] = {}  # absent (older runner) or unreadable: start over
    never_green = state["never_green"]
    probed: set[str] = set()
    loop_errors: list[str] = []

    # One group's run is isolated from the next (#903): a group that raises is named in
    # the heartbeat, the other group still alerts, and the state still saves. The body
    # is a closure so it reads the loop's shared state exactly as the inline loop did.
    def _run_group(group: ProbeGroup) -> None:
        nonlocal any_failures
        specs = _host_specs_for_env(parse_probe_specs(group.raw_specs))
        results = run_probes(specs)
        failures = failed_results(results)
        any_failures = any_failures or bool(failures)
        json_results[group.name] = [result.to_dict() for result in results]

        # Learn which probes have EVER passed (this cycle's passes included).
        for result in results:
            probed.add(result.spec.name)
            if result.ok:
                ever_succeeded.add(result.spec.name)
        _track_never_green(results, ever_succeeded, never_green, now)

        # Cascade suppression: a probe whose declared `depends_on` chain reaches a failing
        # ROOT is a downstream symptom — suppress its alert and page the root only (page the
        # deepest failed node, not the cascade). E.g. signoz-roundtrip fails because the otel
        # collector is down -> page the collector, mute the round-trip.
        # A `depends_on` CYCLE (incl. self-dependency) has no root, so its members are NOT
        # suppressed (fail closed -> alert) — otherwise a cycle of all-failing probes would
        # silently swallow every page.
        failed_names = {r.spec.name for r in failures}
        dep_of = {r.spec.name: r.spec.depends_on for r in failures if r.spec.depends_on}
        cascaded = [
            r
            for r in failures
            if _cascades_to_failing_root(r.spec.name, dep_of, failed_names)
        ]
        cascaded_names = {r.spec.name for r in cascaded}
        for r in cascaded:
            root = _resolve_failing_root(r.spec.name, dep_of, failed_names)
            print(
                f"probe-runner cascade-suppressed probe={r.spec.name} "
                f"depends_on={r.spec.depends_on} root={root} (cascade symptom)",
                flush=True,
            )
        results = [r for r in results if r.spec.name not in cascaded_names]
        failures = [r for r in failures if r.spec.name not in cascaded_names]

        # Split failures into two streams. ONLY `command` probes are eligible for the
        # misconfigured lane: they run code (the round-trips) that can be broken by a bug
        # so they may NEVER pass even against a healthy backend (the signoz-roundtrip
        # 500-storm). `http`/`tcp` liveness probes are NOT eligible — their failure is
        # always a real target failure. A command failure goes to the quiet
        # warning-severity `InfraProbeMisconfigured` lane when
        #   - the probe reported its own configuration missing (EX_CONFIG): nothing about
        #     the target was tested, whatever its history; or
        #   - it has never passed since the runner started AND its failure streak is
        #     still inside the grace period. A live /healthcheck does not prove ingestion
        #     (#726: OpenPanel's stayed 200 through 17 h of NOSCRIPT), so after the
        #     grace period the failure is an outage and joins the normal stream at the
        #     probe's declared severity.
        # Each stream dedups independently.
        misconfig = []
        for r in failures:
            if r.spec.kind != "command":
                continue
            if is_misconfigured(r):
                misconfig.append(r)
            elif r.spec.name not in ever_succeeded:
                streak = never_green.get(r.spec.name)
                if not _escalated(streak, now, escalation_failures, escalation_seconds):
                    misconfig.append(
                        _grace_note(
                            r, group.alert_name, escalation_failures, escalation_seconds
                        )
                    )
                elif streak is not None and not streak.get("escalated"):
                    streak["escalated"] = True
                    print(
                        f"probe-runner escalated probe={r.spec.name} "
                        f"stream={group.name} failed_runs={streak.get('runs')} "
                        f"failing_for={int(now - float(streak.get('since', now)))}s "
                        "(never passed since runner start; no longer 'misconfigured')",
                        flush=True,
                    )
        misconfig_names = {r.spec.name for r in misconfig}
        regression_results = [r for r in results if r.spec.name not in misconfig_names]

        streams = (
            (group.name, regression_results, group.alert_name, None),
            (
                f"{group.name}:misconfigured",
                misconfig,
                MISCONFIGURED_ALERT_NAME,
                MISCONFIGURED_SEVERITY,
            ),
        )
        for stream_key, stream_results, alert_name, severity_override in streams:
            payload = build_probe_alert_payload(
                stream_results,
                alert_name=alert_name,
                external_url=group.external_url,
                severity_override=severity_override,
                now=now,
            )
            if dry_run:
                if failed_results(stream_results):
                    _send_payload(payload)
                continue
            before = copy.deepcopy(state.get("groups", {}).get(stream_key))
            paged = _should_send(
                stream_key,
                stream_results,
                state,
                now,
                renotify_seconds,
                failure_threshold,
                recovery_threshold,
            )
            if paged is None:
                # Nothing pending for this stream: a delivery of it that failed earlier
                # is moot, and must not hold the heartbeat red (#903 review).
                state.get("delivery_failures", {}).pop(stream_key, None)
                continue
            if _maintenance_active(now):
                continue
            payload = build_probe_alert_payload(
                paged,
                alert_name=alert_name,
                external_url=group.external_url,
                severity_override=severity_override,
                now=now,
                **_incident_times(paged, state, stream_key, before),
            )
            if not _send_payload(payload, state, now, stream_key):
                # Undelivered: roll the stream back so the next loop sends it again
                # (a resolve included — _should_send already recorded it).
                _restore_stream(state, stream_key, before)
                continue
            _record_sent(stream_key, paged, state, now)
            _log_send(stream_key, paged, severity_override)

    for group in groups:
        try:
            _run_group(group)
        except Exception as exc:  # noqa: BLE001 - one group must not sink the loop.
            traceback.print_exc()
            loop_errors.append(
                f"group {group.name} raised {type(exc).__name__}: {exc}"[:200]
            )

    state["ever_succeeded"] = sorted(ever_succeeded)
    for name in set(never_green) - probed:
        del never_green[name]  # the probe is gone from the specs
    # One line per cycle, on success too. A probe that passes is silent, so a log with
    # no probe lines in it is indistinguishable from a runner that probed nothing —
    # which is exactly how #608 read this container's log on 2026-09-09, while 24
    # infra-service and 7 public-route probes were in fact passing every minute.
    if not as_json:
        print(
            "infra probes: "
            + " · ".join(
                f"{name} {sum(1 for r in rows if r.get('ok'))}/{len(rows)} ok"
                for name, rows in json_results.items()
            )
            + (" — FAILURES" if any_failures else ""),
            flush=True,
        )
    if as_json:
        print(json.dumps(json_results, indent=2))
    for error in loop_errors:
        print(f"probe-runner loop error: {error}", flush=True)
    if not dry_run:
        _maybe_send_chronic_digest(state, now)
        state["failing_public_routes"] = _paged_public_routes(state, json_results, now)
        problems = list(loop_errors)
        save_error = _save_state(state_path, state)
        record_probe_round(json_results, _deploy_env(), state_path, now)
        if save_error:
            problems.append(f"state save failed: {save_error}")
        delivery_failures = state.get("delivery_failures") or {}
        if delivery_failures:
            problems.append(
                "bridge delivery failed: "
                + "; ".join(
                    f"{error} ({stream_key})"
                    for stream_key, error in sorted(delivery_failures.items())
                )
            )
        healthy = (
            "probe loop completed; alerts suppressed during maintenance"
            if _maintenance_active(now)
            else "probe loop completed"
        )
        _post_heartbeat(
            ok=not problems, detail="; ".join(problems) or healthy, state=state
        )
    return 1 if any_failures or loop_errors else 0


def _probe_groups() -> list[ProbeGroup]:
    # Fail-closed on set-but-empty (#541): os.getenv returns "" (not the default)
    # when the env var EXISTS with an empty value — which is exactly what the
    # compose `${INFRA_PROBE_SPECS:-}` reference produces if the registry renderer
    # ever shipped nothing. Zero probes with a green healthcheck is silent fleet
    # blindness, never a valid state; die loudly so the container goes unhealthy
    # and the breakdown watcher pages, instead of "running" with nothing to do.
    infra_specs = os.getenv("INFRA_PROBE_SPECS")
    if infra_specs is not None and not infra_specs.strip():
        raise SystemExit(
            "INFRA_PROBE_SPECS is set but empty — the registry renderer produced "
            "no probes; refusing to run blind (see #541)"
        )
    groups = [
        ProbeGroup(
            name="infra-service",
            raw_specs=infra_specs if infra_specs is not None else DEFAULT_PROBE_SPECS,
            alert_name="InfraServiceProbeFailed",
            external_url="infra2://platform/12.alerting/infra-probes",
        )
    ]
    public_specs = os.getenv("PUBLIC_ROUTE_PROBE_SPECS", "").strip()
    if public_specs:
        groups.append(
            ProbeGroup(
                name="public-route",
                raw_specs=public_specs,
                alert_name="InfraPublicRouteProbeFailed",
                external_url="infra2://platform/12.alerting/public-route-probes",
            )
        )
    return groups


def _delivers_as_report() -> bool:
    """Only the production runner pages (#903).

    Staging alerts are real signals about staging, but nobody is on call for them:
    every payload the staging runner sends is a REPORT (``delivery=report``). Only an
    environment on the report-only allowlist (staging, the preview slots) reports;
    ``prod``, a typo or an unset value pages rather than going quiet.
    """
    return is_report_only_environment(_deploy_env())


def _send_payload(
    payload: dict,
    state: dict | None = None,
    now: float | None = None,
    stream_key: str | None = None,
) -> bool:
    """POST ``payload`` to the bridge; True on a 2xx.

    With ``state``, the outcome is recorded for the heartbeat: ``last_delivery_ok_at``
    on any success; a failure is kept per stream in ``delivery_failures`` until that
    stream's retry lands or it has nothing left to send.
    """
    if _delivers_as_report():
        payload = mark_report_payload(payload)
    if os.getenv("INFRA_PROBE_DRY_RUN", "0") == "1":
        print(json.dumps(payload, indent=2))
        return True

    bridge_url = os.getenv(
        "ALERT_BRIDGE_URL",
        "http://platform-alerting:8080/signoz/webhook",
    )
    try:
        post_alert_bridge_payload(
            bridge_url,
            payload,
            username=os.getenv("BRIDGE_BASIC_AUTH_USERNAME", ""),
            password=os.getenv("BRIDGE_BASIC_AUTH_PASSWORD", ""),
        )
    except Exception as exc:  # noqa: BLE001 - a failed delivery is reported, not fatal.
        error = _delivery_error(exc)
        print(
            f"probe-runner bridge delivery failed stream={stream_key or '-'}: {error}",
            flush=True,
        )
        if state is not None and stream_key:
            state.setdefault("delivery_failures", {})[stream_key] = error
        return False
    if state is not None:
        state["last_delivery_ok_at"] = int(time.time() if now is None else now)
        if stream_key:
            state.setdefault("delivery_failures", {}).pop(stream_key, None)
    return True


def _delivery_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    return f"{type(exc).__name__}: {exc}"[:200]


def _paged_public_routes(state: dict, json_results: dict, now: float) -> list[str]:
    """The heartbeat's ``failing_public_routes``: public routes failing in this loop
    AND covered by a page this runner delivered that is still open (#903 / #911).

    The Worker stands down its own entrypoint page for every route listed here, so a
    route belongs on the list only once the VPS has actually told someone. It is not
    listed before its debounce threshold, while its page is undelivered (the stream
    state only records what a 2xx delivered), after its page resolved, or at all
    during maintenance, when sends are skipped.
    """
    if _maintenance_active(now):
        return []
    # `probes` is what the last delivered send paged; a resolve empties it.
    paged = set(state.get("groups", {}).get(PUBLIC_ROUTE_GROUP, {}).get("probes") or [])
    failing = {
        row["spec"]["name"]
        for row in json_results.get(PUBLIC_ROUTE_GROUP, [])
        if not row.get("ok")
    }
    return sorted(failing & paged)


def _restore_stream(state: dict, stream_key: str, before: dict | None) -> None:
    groups = state.setdefault("groups", {})
    if before is None:
        groups.pop(stream_key, None)
    else:
        groups[stream_key] = before


def _maybe_send_chronic_digest(state: dict, now: float) -> None:
    """Once per UTC day, report every stream failing for longer than a day (#903).

    The runner no longer re-pages an unchanged incident on a timer, so a long outage
    resurfaces here instead — as a REPORT, the way the breakdown watcher's
    ContainerBreakdownChronic does. The first digest goes out on the loop a stream
    crosses a day; after that, at most one per UTC day. The day is marked only once a
    digest is out, so one held back by maintenance or a failed delivery goes out on a
    later loop the same day.
    """
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if state.get("digest_day") == day:
        state.get("delivery_failures", {}).pop(CHRONIC_DIGEST_ALERT_NAME, None)
        return
    chronic = []
    for stream_key, group_state in sorted(state.get("groups", {}).items()):
        if not group_state.get("active"):
            continue
        # A stream carried over from a runner that did not record the start: from now.
        since = float(group_state.setdefault("active_since", now))
        if now - since > CHRONIC_AFTER_SECONDS:
            chronic.append((stream_key, since, list(group_state.get("probes") or [])))
    if not chronic:
        # nothing to report any more: a digest that failed earlier is moot
        state.get("delivery_failures", {}).pop(CHRONIC_DIGEST_ALERT_NAME, None)
        return
    if _maintenance_active(now):
        return
    payload = _chronic_digest_payload(chronic, now)
    if not _send_payload(payload, state, now, CHRONIC_DIGEST_ALERT_NAME):
        return
    print(
        f"probe-runner CHRONIC-DIGEST count={len(chronic)} -> report, at most once "
        f"per UTC day: {','.join(stream_key for stream_key, _, _ in chronic)}",
        flush=True,
    )
    state["digest_day"] = day


def _chronic_digest_payload(chronic: list, now: float) -> dict:
    environment = _deploy_env()
    alerts = []
    for stream_key, since, probe_names in chronic:
        failing_for = int(now - since)
        alerts.append(
            {
                "status": "firing",
                "labels": {
                    "alertname": CHRONIC_DIGEST_ALERT_NAME,
                    "severity": "warning",
                    "environment": environment,
                    "stream": stream_key,
                },
                "annotations": {
                    "summary": (
                        f"{stream_key} failing for {failing_for // 86400}d "
                        f"{failing_for % 86400 // 3600}h: "
                        + (", ".join(probe_names) or "no probe names recorded")
                    ),
                    "symptom": "still failing: "
                    + (", ".join(probe_names) or "no probe names recorded"),
                    "description": (
                        "not re-paged while its failing set is unchanged; a change "
                        "or the recovery is sent as usual"
                    ),
                },
                "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
            }
        )
    return mark_report_payload(
        {
            "status": "firing",
            "commonLabels": {
                "alertname": CHRONIC_DIGEST_ALERT_NAME,
                "severity": "warning",
                "team": "infra",
                "environment": environment,
            },
            "commonAnnotations": {
                "summary": f"{len(chronic)} probe stream(s) failing for more than a day",
            },
            "groupLabels": {"alertname": CHRONIC_DIGEST_ALERT_NAME},
            "alerts": alerts,
            "externalURL": "infra2://platform/12.alerting/infra-probes",
        }
    )


def _should_send(
    group_name: str,
    results: list,
    state: dict,
    now: float,
    renotify_seconds: int,
    failure_threshold: int,
    recovery_threshold: int,
) -> list | None:
    """Decide whether a stream sends this loop: the results to send, or None.

    Debounce is per probe (#903 review). Each failing probe identity (name, kind,
    failure domain) counts its own consecutive failures, and the stream's PAGE SET is
    the probes past ``failure_threshold`` plus any probe already paged that is still
    failing. A sibling failing every other loop therefore neither holds back a probe
    that is hard down nor re-pages an active incident: only a change of the page set,
    or a positive renotify timer, re-sends. The stream recovers while its page set is
    empty — a probe still below the threshold never paged, so it does not hold the
    incident open.

    The results returned are the page set's failures plus the passing probes (none
    failing for a resolve), so a payload built from them names exactly what paged.
    """
    group_state = state.setdefault("groups", {}).setdefault(group_name, {})
    failing = {_probe_identity(result): result for result in failed_results(results)}
    previous = group_state.get("streaks")
    previous = previous if isinstance(previous, dict) else {}
    streaks = {key: int(previous.get(key) or 0) + 1 for key in failing}
    group_state["streaks"] = streaks
    active = bool(group_state.get("active"))
    paged_names = set(group_state.get("probes") or []) if active else set()
    # When each failure started (#905: the card's 开始于, and how long a recovered
    # probe was down). Kept for a failing identity and, until the stream resolves, for
    # a paged probe that is recovering.
    since = group_state.get("failing_since")
    since = since if isinstance(since, dict) else {}
    group_state["failing_since"] = {
        key: float(value)
        for key, value in since.items()
        if key in failing or key.split("|", 1)[0] in paged_names
    } | {key: float(since.get(key) or now) for key in failing}
    page = {
        key
        for key, result in failing.items()
        if streaks[key] >= max(1, failure_threshold) or result.spec.name in paged_names
    }
    if not page:
        if not active:
            group_state["recovery_count"] = 0
            return None
        recovery_count = int(group_state.get("recovery_count") or 0) + 1
        group_state["recovery_count"] = recovery_count
        if recovery_count < max(1, recovery_threshold):
            return None
        _record_resolved(group_name, state)
        return [result for result in results if result.ok]

    group_state["recovery_count"] = 0
    paged = [
        result for result in results if result.ok or _probe_identity(result) in page
    ]
    last_fingerprint = str(group_state.get("fingerprint") or "")
    last_alert_at = float(group_state.get("last_alert_at") or 0)
    if (
        not active
        or _failure_fingerprint(paged) != last_fingerprint
        # renotify <= 0 turns the timer off (#903): an unchanged incident is not re-paged
        or (renotify_seconds > 0 and now - last_alert_at >= renotify_seconds)
    ):
        return paged
    return None


def _record_sent(group_name: str, results: list, state: dict, now: float) -> None:
    """Record a delivered send. Per-probe streaks survive it: a probe still below the
    threshold keeps counting across the page and the resolve."""
    failures = failed_results(results)
    group_state = state.setdefault("groups", {}).setdefault(group_name, {})
    # When the incident started, kept across re-sends of the same incident, and the
    # probes it pages — what the chronic digest reports (#903).
    active_since = (
        float(group_state.get("active_since") or now)
        if failures and group_state.get("active")
        else (now if failures else 0)
    )
    group_state.update(
        {
            "active": bool(failures),
            "active_since": active_since,
            "probes": sorted(result.spec.name for result in failures),
            "fingerprint": _failure_fingerprint(results) if failures else "",
            "recovery_count": 0,
            "last_alert_at": now,
        }
    )


def _record_resolved(group_name: str, state: dict) -> None:
    state.setdefault("groups", {}).setdefault(group_name, {}).update(
        {
            "active": False,
            "active_since": 0,
            "failing_since": {},
            "probes": [],
            "fingerprint": "",
            "recovery_count": 0,
            "last_alert_at": 0,
        }
    )


def _incident_times(
    paged: list, state: dict, stream_key: str, before: dict | None
) -> dict:
    """What the payload needs to say when (#905): ``started_at`` for a page, from the
    stream's ``failing_since``; ``recovered`` for a resolve — every probe the last
    delivered send paged, with when its failure started (``before`` is the stream as
    it was before this loop resolved it)."""

    def by_name(failing_since: object) -> dict[str, float]:
        starts: dict[str, float] = {}
        for key, value in (failing_since or {}).items():
            name = key.split("|", 1)[0]
            starts[name] = min(float(value), starts.get(name, float(value)))
        return starts

    if failed_results(paged):
        group_state = state.get("groups", {}).get(stream_key, {})
        return {"started_at": by_name(group_state.get("failing_since"))}
    before = before or {}
    starts = by_name(before.get("failing_since"))
    fallback = float(before.get("active_since") or 0) or None
    return {
        "recovered": {
            name: starts.get(name, fallback) for name in before.get("probes") or []
        }
    }


def _probe_identity(result) -> str:
    """One failing probe's identity (#903): which probe, of which kind, failing in
    which failure domain. Its reading is not part of it."""
    return f"{result.spec.name}|{result.spec.kind}|{failure_domain(result)}"


def _failure_fingerprint(results: list) -> str:
    """The IDENTITY of a stream's failing set: which probes, of which kind, failing in
    which failure domain (#903).

    Readings are not identity. A resource probe reports a new percentage every loop and
    an HTTP body can carry a request id; hashing `observed`/`summary` reset the
    debounce every loop (a flapping reading never reached the threshold) and re-sent an
    active incident on every change. They stay in the payload, for display only.
    """
    failures = [
        {
            "name": result.spec.name,
            "kind": result.spec.kind,
            "failure_domain": failure_domain(result),
        }
        for result in failed_results(results)
    ]
    encoded = json.dumps(failures, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"groups": {}}


def _save_state(path: Path, state: dict) -> str:
    """Persist ``state``; returns the error text, or "" when it was written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(f"infra probe state write failed: {exc}", flush=True)
        return f"{type(exc).__name__}: {exc}"
    return ""


def _post_heartbeat(
    *,
    ok: bool,
    detail: str = "",
    liveness: bool = False,
    state: dict | None = None,
) -> None:
    """Publish to the Cloudflare watchdog (contract v2, #903).

    ``ok`` is the probe LOOP's health, never a probe verdict: false only when a group
    run raised, the state save failed, or a send that is still pending failed its
    bridge delivery (it clears when the retry lands or the stream has nothing left to
    send), and ``detail`` names which. ``last_delivery_ok_at`` (epoch seconds of the last bridge
    2xx, 0 if none yet) and ``failing_public_routes`` (routes failing now whose page
    was delivered and is still open) come from ``state``; a liveness
    ping repeats the last completed loop's values. ``liveness`` marks a ping that
    proves the loop is alive but carries no verdict; the watchdog never lets it change
    the stored ``ok``."""
    heartbeat_url = os.getenv("INFRA_PROBE_HEARTBEAT_URL", "").strip()
    if not heartbeat_url:
        return

    state = state or {}
    payload = {
        "env": _deploy_env(),
        "name": os.getenv("INFRA_PROBE_HEARTBEAT_NAME", "infra-probe-runner"),
        "ok": ok,
        "detail": detail or ("probe loop completed" if ok else "probe loop failed"),
        "timestamp": int(time.time()),
        "schema": HEARTBEAT_SCHEMA,
        "last_delivery_ok_at": int(state.get("last_delivery_ok_at") or 0),
        "failing_public_routes": sorted(state.get("failing_public_routes") or []),
    }
    if liveness:
        payload["liveness"] = True
    headers = {**HTTP_PROBE_HEADERS, "Content-Type": "application/json"}
    token = os.getenv("INFRA_PROBE_HEARTBEAT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        heartbeat_url,
        data=json.dumps(payload, sort_keys=True).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(  # noqa: S310 - operator-configured watchdog endpoint.
            request,
            timeout=float(os.getenv("INFRA_PROBE_HEARTBEAT_TIMEOUT", "5")),
        ) as response:
            response.read(1024)
    except OSError as exc:
        print(f"infra probe heartbeat failed: {exc}", flush=True)


def _maintenance_active(now: float | None = None) -> bool:
    raw_until = os.getenv("INFRA_PROBE_MAINTENANCE_UNTIL", "").strip()
    if not raw_until:
        return False
    try:
        until = float(raw_until)
    except ValueError:
        return False
    return (time.time() if now is None else now) < until


#: Keys this process took from the env file. Only these are refreshed on a
#: re-read; a non-empty value set by the compose environment still wins (#915).
_ENV_FILE_KEYS: set[str] = set()
#: Whether the last read found the file, so a missing file is logged on change only.
_ENV_FILE_SEEN: list[bool | None] = [None]
#: Keys last reported as no longer rendered, so the log fires on change only.
_ENV_FILE_VANISHED: list[tuple[str, ...]] = [()]


def _load_env_file(path: Path) -> bool:
    """Load (or re-load) the Vault-rendered env file; False when it is unreadable.

    Called at startup and before every loop iteration (#915): vault-agent
    re-renders the file when a secret changes (its entrypoint also deletes it
    on every restart), so a runner that read it once could come up before the
    file existed and keep empty probe credentials for its whole life.

    Never raises: the loop that calls it also carries the resident watchers.
    A failed or partial read keeps the values already loaded -- an empty value
    never replaces a good one, and a key missing from one render keeps its old
    value (logged): a revoked credential still shows up as a failing probe,
    while a torn read never wipes working ones.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        if _ENV_FILE_SEEN[0] is not False:
            print(
                f"infra probe env file {path} is unreadable ({type(exc).__name__}); "
                "keeping the values already loaded",
                flush=True,
            )
        _ENV_FILE_SEEN[0] = False
        return False
    if _ENV_FILE_SEEN[0] is False:
        print(f"infra probe env file {path} is readable again", flush=True)
    _ENV_FILE_SEEN[0] = True
    rendered: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        rendered.add(key)
        if not value:
            continue
        if key in _ENV_FILE_KEYS or not os.environ.get(key):
            os.environ[key] = value
            _ENV_FILE_KEYS.add(key)
    vanished = tuple(sorted(_ENV_FILE_KEYS - rendered))
    if vanished and vanished != _ENV_FILE_VANISHED[0]:
        print(
            f"infra probe env file {path} no longer renders {', '.join(vanished)}; "
            "keeping the last values",
            flush=True,
        )
    _ENV_FILE_VANISHED[0] = vanished
    return True


if __name__ == "__main__":
    raise SystemExit(main())
