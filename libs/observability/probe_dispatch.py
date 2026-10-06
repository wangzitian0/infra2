"""Infra probe dispatch, state management, and failure fingerprinting.

Part of libs.observability domain modularization.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError

from libs.alerting.card import mark_report_payload
from libs.observability.probes import failed_results, failure_domain

PUBLIC_ROUTE_GROUP = "public-route"
CHRONIC_DIGEST_ALERT_NAME = "InfraProbeChronic"
CHRONIC_AFTER_SECONDS = 24 * 3600

__all__ = [
    "CHRONIC_AFTER_SECONDS",
    "CHRONIC_DIGEST_ALERT_NAME",
    "PUBLIC_ROUTE_GROUP",
    "_chronic_digest_payload",
    "_delivery_error",
    "_failure_fingerprint",
    "_incident_times",
    "_load_state",
    "_maintenance_active",
    "_paged_public_routes",
    "_probe_identity",
    "_record_resolved",
    "_record_sent",
    "_restore_stream",
    "_save_state",
    "_should_send",
]


def _maintenance_active(now: float | None = None) -> bool:
    raw_until = os.getenv("INFRA_PROBE_MAINTENANCE_UNTIL", "").strip()
    if not raw_until:
        return False
    try:
        until = float(raw_until)
    except ValueError:
        return False
    return (time.time() if now is None else now) < until


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


def _chronic_digest_payload(
    chronic: list,
    now: float,
    environment: str | None = None,
) -> dict:
    env = (
        environment
        if environment is not None
        else os.getenv("DEPLOY_ENV", "prod").strip().lower()
    )
    alerts = []
    for stream_key, since, probe_names in chronic:
        failing_for = int(now - since)
        alerts.append(
            {
                "status": "firing",
                "labels": {
                    "alertname": CHRONIC_DIGEST_ALERT_NAME,
                    "severity": "warning",
                    "environment": env,
                    "stream": stream_key,
                },
                "annotations": {
                    "summary": (
                        f"{stream_key} failing for {failing_for // 86400}d "
                        f"{failing_for % 86400 // 3600}h: "
                        + (", ".join(probe_names) or "no probe names recorded")
                    ),
                    "symptom": "仍在失败:" + (", ".join(probe_names) or "未记录探针名"),
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
                "environment": env,
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
