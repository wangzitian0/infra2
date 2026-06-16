#!/usr/bin/env python3
"""Client for the iac_runner ``/deploy`` webhook — the platform-service deploy trigger.

``deploy_v2`` routes platform (iac-pinned) services HERE rather than re-implementing their
deploy: ``Deployer.sync`` is deeply invoke/Context + ``os.environ`` coupled, so the faithful
move is to trigger the SAME signed webhook ``deploy-platform.yml`` already uses. A platform
deploy via ``deploy_v2`` is therefore byte-for-byte the deploy iac_runner performs today —
fidelity by construction, not by replication.

Signing mirrors ``webhook_server.verify_iac_signature`` exactly:
    signed_payload = f"{timestamp}.{nonce}.".encode() + payload_bytes
    X-Hub-Signature-256: sha256=HMAC_SHA256(IAC_WEBHOOK_SECRET, signed_payload)
    + X-IAC-Timestamp: <unix seconds> , X-IAC-Nonce: <hex>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time

import httpx

_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_VALID_ENVS = ("staging", "production")


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
) -> dict:
    """Trigger an iac_runner platform deploy of ``services`` at ``ref`` to ``env``.

    ``env`` is ``staging``|``production``; ``ref`` a 40-hex infra2 commit (the iac_ref);
    ``services`` the short service names (``["redis"]``, or ``["__all__"]``). Returns the
    webhook's JSON response. Raises ``ValueError`` for a bad env/ref/secret before any POST,
    and ``httpx.HTTPError`` on transport / non-2xx. ``transport`` is injected for tests.
    """
    if env not in _VALID_ENVS:
        raise ValueError(f"env must be one of {_VALID_ENVS}, got {env!r}")
    if not _SHA40_RE.match(ref or ""):
        raise ValueError(f"ref must be a 40-hex commit sha, got {ref!r}")
    if not secret:
        raise ValueError("IAC_WEBHOOK_SECRET is required to sign the deploy request")
    if not base_url:
        raise ValueError("iac_runner base_url is required")

    payload = json.dumps(
        {
            "env": env,
            "ref": ref,
            "triggered_by": triggered_by,
            "wait": wait,
            "services": list(services),
        },
        separators=(",", ":"),
    ).encode()
    headers = _signed_headers(secret, payload, now=now, nonce=nonce or _new_nonce())
    resp = transport(
        f"{base_url.rstrip('/')}/deploy",
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
) -> dict:
    """Poll ``/deploy/status`` until the deploy reaches a terminal state (mirrors the bash loop).

    Returns the final status dict. A terminal status is anything other than ``running`` /
    ``pending`` / ``in_progress``. Raises ``TimeoutError`` if it never settles within
    ``attempts``.
    """
    payload = json.dumps(
        {"env": env, "ref": ref, "triggered_by": triggered_by},
        separators=(",", ":"),
    ).encode()
    terminal_excluded = {"running", "pending", "in_progress", "queued"}
    last: dict = {}
    for _ in range(max(1, attempts)):
        headers = _signed_headers(secret, payload, now=now, nonce=nonce_factory())
        resp = transport(
            f"{base_url.rstrip('/')}/deploy/status",
            content=payload,
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        last = resp.json() if resp.content else {}
        if str(last.get("status", "")).lower() not in terminal_excluded:
            return last
        sleep(interval)
    raise TimeoutError(
        f"iac_runner deploy {ref[:12]} to {env} did not settle within {attempts} polls "
        f"(last status={last.get('status')!r})"
    )
