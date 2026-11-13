#!/usr/bin/env python3
"""Client for the iac_runner /deploy and /preview webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_DEPLOYMENT_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
# The one table of values each route accepts; webhook_server.py reads the same file.
ROUTE_TYPES: dict[str, list[str]] = json.loads(
    (
        Path(__file__).resolve().parents[2] / "bootstrap/06.iac_runner/route_types.json"
    ).read_text(encoding="utf-8")
)

STATUS_POLL_INITIAL_SECONDS = 2.0
STATUS_POLL_BACKOFF = 1.25
STATUS_POLL_MAX_SECONDS = 10.0
STATUS_NOT_FOUND_GRACE_SECONDS = 90.0


class RunnerLostDeploymentError(RuntimeError):
    """The runner has no record of an accepted deploy."""


class RunnerInterruptedError(RunnerLostDeploymentError):
    """The runner restarted while a deploy was in flight."""


def status_poll_delays(
    initial: float = STATUS_POLL_INITIAL_SECONDS,
    backoff: float = STATUS_POLL_BACKOFF,
    maximum: float = STATUS_POLL_MAX_SECONDS,
) -> Iterator[float]:
    """Yield successive pauses between status polls."""
    if initial < 0 or maximum < 0 or backoff < 1:
        raise ValueError(
            "poll delays need initial >= 0, maximum >= 0 and backoff >= 1, got "
            f"initial={initial!r} maximum={maximum!r} backoff={backoff!r}"
        )

    def delays() -> Iterator[float]:
        delay = min(float(initial), float(maximum))
        while True:
            yield delay
            delay = min(delay * backoff, float(maximum))

    return delays()


def status_poll_attempts(
    budget_seconds: float,
    *,
    initial: float = STATUS_POLL_INITIAL_SECONDS,
    backoff: float = STATUS_POLL_BACKOFF,
    maximum: float = STATUS_POLL_MAX_SECONDS,
) -> int:
    """Calculate the number of polls required to cover the budget."""
    budget = max(0.0, float(budget_seconds))
    if initial <= 0 or maximum <= 0:
        raise ValueError("a poll schedule that never pauses cannot cover a time budget")
    delays = status_poll_delays(initial, backoff, maximum)
    attempts, waited = 1, next(delays)
    while waited < budget:
        attempts += 1
        waited += next(delays)
    return attempts


def _sign(secret: str, timestamp: str, nonce: str, payload: bytes) -> str:
    """The X-Hub-Signature-256 value for (timestamp, nonce, payload). See module docstring."""
    signed_payload = f"{timestamp}.{nonce}.".encode() + payload
    return (
        "sha256="
        + hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    )


def _signed_headers(secret: str, payload: bytes, *, now, nonce: str) -> dict[str, str]:
    timestamp = str(int(now()))
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _sign(secret, timestamp, nonce, payload),
        "X-IAC-Timestamp": timestamp,
        "X-IAC-Nonce": nonce,
    }


def _new_nonce() -> str:
    # openssl rand -hex 16 -> 32 hex chars; matches the server's [A-Za-z0-9._:-]{8,128}.
    return os.urandom(16).hex()


def _safe_json(resp) -> dict:
    """Best-effort JSON body as a dict; {} on empty/non-JSON/non-object (e.g. an HTML 404)."""
    try:
        data = resp.json() if getattr(resp, "content", None) else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _json_object_or_none(resp) -> dict | None:
    """The response body as a dict, or ``None`` when there is no parseable JSON object.

    Distinct from :func:`_safe_json`, which collapses "no body" and "an empty JSON
    object" into the same ``{}`` — the gateway check below has to tell a bodiless
    Traefik 404 apart from a runner 404 that really did answer ``{}``.
    """
    try:
        data = resp.json() if getattr(resp, "content", None) else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _validate_request(
    env: str, ref: str, secret: str, base_url: str, route: str = "/deploy"
) -> None:
    """Fail closed BEFORE signing/posting — never sign with an empty secret or bad target."""
    if env not in ROUTE_TYPES[route]:
        raise ValueError(f"env must be one of {ROUTE_TYPES[route]}, got {env!r}")
    if not _SHA40_RE.match(ref or ""):
        raise ValueError(f"ref must be a 40-hex commit sha, got {ref!r}")
    if not secret:
        raise ValueError(
            "IAC_WEBHOOK_SECRET (/deploy) or IAC_PREVIEW_WEBHOOK_SECRET (/preview) "
            "is required to sign the request"
        )
    if not base_url:
        raise ValueError("iac_runner base_url is required")


_VERSION_REF_RE = re.compile(r"\A(v[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{7,40})\Z")


def _validate_version_ref(version_ref: str) -> str:
    """A release tag or a commit sha — the only two shapes a Deployer may pin from."""
    candidate = str(version_ref).strip()
    if not _VERSION_REF_RE.match(candidate):
        raise ValueError(
            f"version_ref must be a vX.Y.Z tag or a 7-40 hex commit sha, got {version_ref!r}"
        )
    return candidate


def _fetch_actions_oidc_token(
    *, audience: str = "iac-runner", getter=httpx.get
) -> str | None:
    """Request a GitHub Actions OIDC token when running inside an OIDC-enabled job (#1126)."""
    explicit = os.environ.get("IAC_OIDC_TOKEN", "").strip()
    if explicit:
        return explicit
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not (req_url and req_token):
        return None
    sep = "&" if "?" in req_url else "?"
    resp = getter(
        f"{req_url}{sep}audience={audience}",
        headers={"Authorization": f"Bearer {req_token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = _safe_json(resp)
    value = str(data.get("value") or "").strip()
    if not value:
        raise RuntimeError("GitHub OIDC token endpoint returned no token value")
    return value


def trigger_platform_deploy(
    *,
    env: str,
    ref: str,
    services: list[str],
    base_url: str,
    secret: str,
    triggered_by: str = "deploy_v2",
    wait: bool = False,
    timeout: float = 60.0,
    now=time.time,
    nonce: str | None = None,
    transport=httpx.post,
    version_ref: str | None = None,
    action: str | None = None,
    oidc_token: str | None = None,
    route: str = "/deploy",
    extra: dict | None = None,
) -> dict:
    """Trigger an iac_runner platform deploy for specified services.

    ``route`` is the endpoint path. A ``/preview`` call names the Preview type in ``env``,
    carries ``extra`` (pr, branch, down) and must be signed with the Preview secret.
    """
    _validate_request(env, ref, secret, base_url, route)
    if not all(isinstance(service, str) for service in services):
        raise ValueError("services must be a non-empty list of non-empty strings")
    normalized_services = sorted({service.strip() for service in services})
    if not normalized_services or any(not service for service in normalized_services):
        raise ValueError("services must be a non-empty list of non-empty strings")

    body: dict = {
        "env" if route == "/deploy" else "type": env,
        "ref": ref,
        "triggered_by": triggered_by,
        "wait": wait,
        "services": normalized_services,
        **({"action": action} if action and action != "sync" else {}),
        **(extra or {}),
    }
    if version_ref is not None:
        # The app release a digest-pinned platform service should pin (truealpha#712);
        # validated here so a malformed ref fails before any POST, like env/ref.
        body["version_ref"] = _validate_version_ref(version_ref)
    payload = json.dumps(body, separators=(",", ":")).encode()
    headers = _signed_headers(secret, payload, now=now, nonce=nonce or _new_nonce())
    if env == "production":
        token = oidc_token or _fetch_actions_oidc_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    resp = transport(
        f"{base_url.rstrip('/')}{route}",
        content=payload,
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def poll_platform_deploy_status(
    *,
    env: str,
    ref: str,
    services: list[str] | None = None,
    deployment_id: str | None = None,
    base_url: str,
    secret: str,
    triggered_by: str = "deploy_v2",
    attempts: int = 90,
    interval: float = 10.0,
    timeout: float = 60.0,
    now=time.time,
    sleep=time.sleep,
    nonce_factory=_new_nonce,
    transport=httpx.post,
    version_ref: str | None = None,
    action: str | None = None,
    gateway_grace: float = 180.0,
    not_found_grace: float = STATUS_NOT_FOUND_GRACE_SECONDS,
    backoff: float = 1.0,
    max_interval: float | None = None,
    route: str = "/deploy",
    extra: dict | None = None,
) -> dict:
    """Poll ``{route}/status`` until the deploy reaches a terminal state."""
    _validate_request(env, ref, secret, base_url, route)
    if services is not None and not all(
        isinstance(service, str) for service in services
    ):
        raise ValueError("services must be a non-empty list of non-empty strings")
    normalized_services = (
        sorted({service.strip() for service in services})
        if services is not None
        else None
    )
    if normalized_services is not None and (
        not normalized_services or any(not service for service in normalized_services)
    ):
        raise ValueError("services must be a non-empty list of non-empty strings")
    if deployment_id is not None and not _DEPLOYMENT_ID_RE.match(deployment_id):
        raise ValueError("deployment_id must be 16 lowercase hex characters")
    status_coordinate = {
        "env" if route == "/deploy" else "type": env,
        "ref": ref,
        "triggered_by": triggered_by,
        **(extra or {}),
    }
    if normalized_services is not None:
        status_coordinate["services"] = normalized_services
    if action and action != "sync":
        status_coordinate["action"] = action
    if version_ref is not None:
        status_coordinate["version_ref"] = _validate_version_ref(version_ref)
    if deployment_id is not None:
        status_coordinate["deployment_id"] = deployment_id
    payload = json.dumps(
        status_coordinate,
        separators=(",", ":"),
    ).encode()
    # non-terminal statuses from /deploy/status (terminal = "completed" / "failed").
    terminal_excluded = {"running", "pending", "in_progress", "queued", "accepted"}
    gateway_codes = {502, 503, 504, 520, 521, 522, 523, 524}
    delays = status_poll_delays(
        interval, backoff, interval if max_interval is None else max_interval
    )
    last: dict = {}
    gateway_down_since: float | None = None
    not_found_since: float | None = None
    for _ in range(max(1, attempts)):
        headers = _signed_headers(secret, payload, now=now, nonce=nonce_factory())
        try:
            resp = transport(
                f"{base_url.rstrip('/')}{route}/status",
                content=payload,
                headers=headers,
                timeout=timeout,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            moment = float(now())
            gateway_down_since = (
                gateway_down_since if gateway_down_since is not None else moment
            )
            if moment - gateway_down_since > gateway_grace:
                raise RuntimeError(
                    f"iac_runner unreachable for {int(moment - gateway_down_since)}s while "
                    f"polling deploy {ref[:12]} to {env} (last error: {exc}): the runner was "
                    "probably recreated by a bootstrap push mid-deploy (#666) or gateway timed out"
                ) from exc
            sleep(next(delays))
            continue
        # iac_runner answers 404 {"status":"not_found"} when the deploy isn't visible
        # yet: the trigger's in-flight entry hasn't registered, or the runner restarted
        # and lost its in-memory deploy state. That's a TRANSIENT miss right after firing
        # (wait=False), not a terminal failure — a single 404 must not crash the whole
        # reconcile, so we treat it as non-terminal and keep polling. A genuine routing
        # 404 (no JSON body / different status) still surfaces via raise_for_status().
        # The tolerance has a bound: after `not_found_grace` seconds of consecutive
        # not_found answers the request is lost, so the poll fails (#666). Only a real
        # status resets the timer; a gateway error does not (the gateway can flap).
        code = getattr(resp, "status_code", None)
        body = _json_object_or_none(resp) if code == 404 else None
        if code == 404 and body is not None:
            if str(body.get("status", "")).lower() == "not_found":
                last = body
                gateway_down_since = None
                moment = float(now())
                not_found_since = (
                    not_found_since if not_found_since is not None else moment
                )
                if moment - not_found_since > not_found_grace:
                    lost_id = deployment_id or body.get("deployment_id") or "unknown"
                    raise RunnerLostDeploymentError(
                        f"iac_runner answered not_found for {int(moment - not_found_since)}s "
                        f"while polling deploy {ref[:12]} to {env} (deployment {lost_id}): "
                        "the runner has no record of this deployment; it was probably "
                        "recreated between the request and the first poll (#666)"
                    )
                sleep(next(delays))
                continue
        # The runner is being recreated (#666) or Cloudflare timed out to origin:
        # Traefik answers a bodiless/non-JSON 404 while no router exists, then 502/503/504
        # or Cloudflare 520-524 until the container is healthy/reachable.
        # A 404 that carries a JSON object which is not `not_found` — including an empty
        # one — is a genuine routing error and still raises below.
        if code in gateway_codes or (code == 404 and body is None):
            moment = float(now())
            gateway_down_since = (
                gateway_down_since if gateway_down_since is not None else moment
            )
            if moment - gateway_down_since > gateway_grace:
                raise RuntimeError(
                    f"iac_runner unreachable for {int(moment - gateway_down_since)}s while "
                    f"polling deploy {ref[:12]} to {env} (last HTTP {code}): the runner was "
                    "probably recreated by a bootstrap push mid-deploy (#666) or gateway / Cloudflare timed out"
                )
            sleep(next(delays))
            continue
        gateway_down_since = None
        not_found_since = None
        resp.raise_for_status()
        last = resp.json() if resp.content else {}
        if str(last.get("status", "")).lower() == "interrupted":
            raise RunnerInterruptedError(
                f"iac_runner reported interrupted for deploy {ref[:12]} to {env} "
                f"(deployment {deployment_id or last.get('deployment_id') or 'unknown'}): "
                "the runner restarted while it ran this deployment (#1439)"
            )
        if str(last.get("status", "")).lower() not in terminal_excluded:
            return last
        sleep(next(delays))
    raise TimeoutError(
        f"iac_runner deploy {ref[:12]} to {env} did not settle within {attempts} polls "
        f"(last status={last.get('status')!r})"
    )


__all__ = [
    "ROUTE_TYPES",
    "STATUS_NOT_FOUND_GRACE_SECONDS",
    "STATUS_POLL_BACKOFF",
    "STATUS_POLL_INITIAL_SECONDS",
    "STATUS_POLL_MAX_SECONDS",
    "poll_platform_deploy_status",
    "status_poll_attempts",
    "status_poll_delays",
    "trigger_platform_deploy",
]
