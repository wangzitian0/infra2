#!/usr/bin/env python3
"""
GitHub Webhook Server for IaC Runner

Receives GitHub push events and triggers sync for changed services.
"""

import base64
import contextlib
import functools
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WORKSPACE = Path("/workspace")
GIT_REPO_URL = os.environ.get("GIT_REPO_URL")
GIT_BRANCH = os.environ.get("GIT_BRANCH") or "main"
SECRETS_FILE = Path("/secrets/.env")
RECENT_DEPLOY_TTL_SECONDS = 600
# A deploy that was in flight when the runner restarted reads back as "interrupted". The
# client may poll long after the restart (it tolerates a 180 s gateway outage first), so
# this record outlives an ordinary result.
INTERRUPTED_DEPLOY_TTL_SECONDS = 3600
# Deploy records live on the /workspace volume so a worker or container restart does not
# erase them (#1439). The directory sits beside the repo checkout, never inside it: a
# `git clean` of /workspace/infra2 must not remove it. Tests point it elsewhere.
DEPLOY_STATE_FILE = (
    Path(os.environ.get("IAC_RUNNER_STATE_DIR") or WORKSPACE / ".iac_runner")
    / "deploy_state.json"
)
DEPLOY_STATE_VERSION = 1
MAX_PERSISTED_DEPLOYS = 64
SIGNATURE_TTL_SECONDS = 300
MAX_REQUEST_BODY_BYTES = 65536
EXACT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
DEPLOYMENT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
REQUIRED_PYTHON_MODULES = {
    "flask": "flask",
    "httpx": "httpx",
    "invoke": "invoke",
    "python-dotenv": "dotenv",
    "PyYAML": "yaml",
    "rich": "rich",
}
REQUIRED_BINARIES = ("git", "op")
# 1Password service-account health is FUNCTIONAL, not presence-only: a deleted/invalid SA
# leaves OP_SERVICE_ACCOUNT_TOKEN set but op broken — the silent-green failure behind the
# #284 outage. `op whoami` actually authenticates the SA token (fails on a deleted/invalid
# SA, the real incident), without assuming vault-access semantics. Throttled (token validity
# is slow-changing; don't call the 1Password API on every /health hit — same reasoning as
# the vault-agent lookup throttle, #292).
OP_HEALTHCHECK_TTL_SECONDS = 300
_op_health_lock = threading.Lock()
# `at` starts at -inf (always stale under monotonic time) so the FIRST /health after process
# start runs the real op check instead of serving the default ok=False — otherwise a fresh
# container whose monotonic clock is < TTL would report a false 503 for up to the TTL window.
_op_health_cache: dict[str, object] = {"ok": False, "at": float("-inf")}

_deploy_state_lock = threading.Lock()
DeploymentKey = tuple[str, str, tuple[str, ...]]
_in_flight_deploys: set[DeploymentKey] = set()
# Wall-clock start and trigger of each in-flight deploy; only the persisted record reads it.
_in_flight_meta: dict[DeploymentKey, tuple[float, str]] = {}
_recent_deploys: dict[DeploymentKey, tuple[float, dict]] = {}
# The last error from writing the state file; "" while the file is current (#1439).
_deploy_state_error = ""
_seen_nonces: dict[str, float] = {}
_nonce_lock = threading.Lock()
if hasattr(app, "config"):
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BODY_BYTES

if not GIT_REPO_URL:
    raise RuntimeError("GIT_REPO_URL environment variable must be set")


def verify_signature(payload: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET is not configured")
        return False

    if not signature or not signature.startswith("sha256="):
        return False

    expected = (
        "sha256="
        + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    )

    return hmac.compare_digest(expected, signature)


def verify_iac_signature(
    payload: bytes, signature: str, timestamp: str, nonce: str
) -> bool:
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET is not configured")
        return False
    if not timestamp or not nonce:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", nonce):
        return False
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False
    now = int(time.time())
    if abs(now - timestamp_int) > SIGNATURE_TTL_SECONDS:
        return False

    signed_payload = f"{timestamp}.{nonce}.".encode() + payload
    expected = (
        "sha256="
        + hmac.new(WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256).hexdigest()
    )
    if not signature or not hmac.compare_digest(expected, signature):
        return False

    nonce_key = f"{timestamp}:{nonce}"
    with _nonce_lock:
        expired = [
            key
            for key, seen_at in _seen_nonces.items()
            if now - seen_at > SIGNATURE_TTL_SECONDS
        ]
        for key in expired:
            _seen_nonces.pop(key, None)
        if nonce_key in _seen_nonces:
            return False
        _seen_nonces[nonce_key] = now
    return True


def verify_iac_request() -> bool:
    return verify_iac_signature(
        request.data,
        request.headers.get("X-Hub-Signature-256", ""),
        request.headers.get("X-IAC-Timestamp", ""),
        request.headers.get("X-IAC-Nonce", ""),
    )


GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = f"{GITHUB_OIDC_ISSUER}/.well-known/jwks"
OIDC_ALLOWED_REPO = "wangzitian0/infra2"
OIDC_ALLOWED_AUDIENCE = "iac-runner"
OIDC_REF_RE = re.compile(r"\A(refs/heads/main|refs/tags/v.+)\Z")
OIDC_JWKS_TTL_SECONDS = 3600
_jwks_lock = threading.Lock()
_jwks_cache: dict[str, object] = {"keys": [], "at": float("-inf")}


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _fetch_github_jwks() -> list[dict]:
    now = time.monotonic()
    with _jwks_lock:
        if (
            now - float(_jwks_cache["at"]) < OIDC_JWKS_TTL_SECONDS
            and _jwks_cache["keys"]
        ):
            return list(_jwks_cache["keys"])  # type: ignore[arg-type]
    with urllib.request.urlopen(GITHUB_OIDC_JWKS_URL, timeout=10) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    keys = doc.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError("GitHub OIDC JWKS returned no keys")
    with _jwks_lock:
        _jwks_cache["keys"] = keys
        _jwks_cache["at"] = now
    return keys


def _verify_rs256_jwk(signing_input: bytes, signature: bytes, jwk: dict) -> bool:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    if jwk.get("kty") != "RSA":
        return False
    n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
    e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    public_key = rsa.RSAPublicNumbers(e, n).public_key()
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:  # noqa: BLE001
        return False


def verify_github_oidc_token(
    auth_header: str,
    *,
    now=time.time,
    jwks_fetcher=None,
) -> tuple[bool, str, dict]:
    """Verify a GitHub Actions OIDC Bearer token for production deploys (#1126)."""
    if jwks_fetcher is None:
        jwks_fetcher = _fetch_github_jwks
    if not auth_header or not auth_header.startswith("Bearer "):
        return (
            False,
            "Production deploy requires a GitHub Actions OIDC Bearer token",
            {},
        )
    token = auth_header.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return False, "Malformed OIDC token", {}
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        signature = _b64url_decode(parts[2])
    except Exception:  # noqa: BLE001
        return False, "Malformed OIDC token encoding", {}
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return False, "Malformed OIDC token claims", {}
    if header.get("alg") != "RS256" or not header.get("kid"):
        return False, "Unsupported OIDC token algorithm or missing kid", {}

    try:
        keys = jwks_fetcher()
    except Exception as exc:  # noqa: BLE001
        return False, f"OIDC JWKS fetch failed: {exc}", {}

    kid = str(header["kid"])
    jwk = next(
        (k for k in keys if isinstance(k, dict) and str(k.get("kid")) == kid), None
    )
    if jwk is None:
        return False, "OIDC signing key not found in GitHub JWKS", {}

    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    if not _verify_rs256_jwk(signing_input, signature, jwk):
        return False, "Invalid OIDC token signature", {}

    current_time = float(now())
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or current_time >= float(exp):
        return False, "OIDC token has expired", {}
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and current_time + 30 < float(nbf):
        return False, "OIDC token is not yet valid", {}
    if claims.get("iss") != GITHUB_OIDC_ISSUER:
        return False, f"Unexpected OIDC issuer: {claims.get('iss')!r}", {}
    aud_claim = claims.get("aud")
    aud_ok = (
        aud_claim == OIDC_ALLOWED_AUDIENCE
        if isinstance(aud_claim, str)
        else (
            isinstance(aud_claim, list)
            and OIDC_ALLOWED_AUDIENCE in [str(a) for a in aud_claim]
        )
    )
    if not aud_ok:
        return (
            False,
            f"Unexpected OIDC audience: {aud_claim!r} (expected {OIDC_ALLOWED_AUDIENCE!r})",
            {},
        )
    if claims.get("repository") != OIDC_ALLOWED_REPO:
        return False, f"Unexpected OIDC repository: {claims.get('repository')!r}", {}
    if claims.get("environment") != "production":
        return (
            False,
            f"OIDC token environment must be 'production', got {claims.get('environment')!r}",
            {},
        )
    ref_claim = str(claims.get("ref") or "")
    if not OIDC_REF_RE.fullmatch(ref_claim):
        return (
            False,
            f"OIDC token ref {ref_claim!r} does not match refs/heads/main or refs/tags/v*",
            {},
        )
    return True, "", claims


def validate_deploy_ref(ref: str | None) -> str | None:
    if not isinstance(ref, str):
        return None
    normalized = ref.strip().lower()
    return normalized if EXACT_COMMIT_RE.fullmatch(normalized) else None


def parse_bool(value) -> bool:
    """Parse booleans from JSON or common string literals."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    raise ValueError("wait must be a boolean")


def _normalize_services(services: list[str] | None) -> tuple[str, ...]:
    """Canonical service-set identity; omitted services means the all-services operation."""
    if services is None:
        return ("__all__",)
    return tuple(sorted({service.strip() for service in services}))


VERSION_REF_RE = re.compile(r"\A(v[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{7,40})\Z")


def validate_version_ref(value) -> str | None:
    """Optional app release the platform deploy should pin (truealpha#712): a vX.Y.Z
    tag or a commit sha, or None when absent. Anything else is a 400, never coerced."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not VERSION_REF_RE.fullmatch(value.strip()):
        raise ValueError("version_ref must be a vX.Y.Z tag or a 7-40 hex commit sha")
    return value.strip()


DEPLOY_ACTIONS = ("sync", "secrets-supply")


def _deployment_key(
    env: str,
    ref: str,
    services: list[str] | None = None,
    version_ref: str | None = None,
    action: str = "sync",
) -> DeploymentKey:
    # version_ref is part of the key: two promotions of different app releases on the
    # same iac_ref within RECENT_DEPLOY_TTL_SECONDS are different deployments.
    # The action is part of the key too: a secrets-supply request must never be
    # answered from a cached sync of the same coordinates (#649).
    return (
        env,
        ref,
        _normalize_services(services),
        version_ref or "",
        action or "sync",
    )


def _action_kwargs(action: str | None) -> dict:
    """Only a non-default action travels as a kwarg (legacy fakes keep their shape)."""
    return {"action": action} if action and action != "sync" else {}


def _deployment_id(key: DeploymentKey) -> str:
    service_identity = ",".join(key[2])
    version_identity = key[3] if len(key) > 3 and key[3] else ""
    # Legacy ids (no version_ref) keep their exact formula so a client that polls by the
    # id it was handed before this change still finds its deployment.
    seed = f"{key[0]}:{key[1]}:{service_identity}"
    if version_identity:
        seed += f":{version_identity}"
    if len(key) > 4 and key[4] and key[4] != "sync":
        seed += f":{key[4]}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _record_ttl(response: dict) -> int:
    """How long a remembered record stays: an interrupted one outlives a result (#1439)."""
    if response.get("status") == "interrupted":
        return INTERRUPTED_DEPLOY_TTL_SECONDS
    return RECENT_DEPLOY_TTL_SECONDS


def _recent_result(key: DeploymentKey) -> dict | None:
    item = _recent_deploys.get(key)
    if not item:
        return None
    completed_at, response = item
    if time.monotonic() - completed_at > _record_ttl(response):
        _recent_deploys.pop(key, None)
        return None
    return response


# --- Persistent deploy records (#1439) -------------------------------------------------
#
# gunicorn runs one worker with four threads, so every writer below is a thread of one
# process and `_deploy_state_lock` orders them. A snapshot and its write happen under that
# one lock: two writers that snapshot in one order and write in the other would leave the
# older state on disk. The file is replaced atomically (temp file in the same directory,
# fsync, os.replace), so a reader or a crash sees the old complete file or the new one.
#
# What is saved: the keys in flight, and the recent results. A restart kills the threads
# that ran the in-flight deploys, so at load every in-flight key becomes an "interrupted"
# record. Before this change it became "not_found", and the client could not tell a lost
# request from a deploy that never existed (run 41967457003, 2026-11-05).


def _key_to_json(key: DeploymentKey) -> dict:
    return {
        "env": key[0],
        "ref": key[1],
        "services": list(key[2]),
        "version_ref": key[3],
        "action": key[4],
    }


def _key_from_json(raw) -> DeploymentKey | None:
    """The key a record was saved under, or None when the entry is not a valid key."""
    if not isinstance(raw, dict):
        return None
    env, ref, services = raw.get("env"), raw.get("ref"), raw.get("services")
    version_ref, action = raw.get("version_ref", ""), raw.get("action", "sync")
    if not (isinstance(env, str) and isinstance(ref, str)):
        return None
    if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
        return None
    if not (isinstance(version_ref, str) and isinstance(action, str)):
        return None
    return (env, ref, tuple(services), version_ref, action or "sync")


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _interrupted_response(key: DeploymentKey, interrupted_at: float, triggered_by: str):
    response = {
        "status": "interrupted",
        "deployment_id": _deployment_id(key),
        "env": key[0],
        "ref": key[1],
        "triggered_by": triggered_by,
        "requested_services": list(key[2]),
        "interrupted_at": _iso_utc(interrupted_at),
        "error": (
            "iac_runner restarted while this deployment was in flight, so its outcome "
            "is unknown. Request it again: a service whose configuration hash already "
            "matches is skipped, and any other service is deployed."
        ),
    }
    if key[3]:
        response["version_ref"] = key[3]
    if key[4] and key[4] != "sync":
        response["action"] = key[4]
    return response


def _prune_recent_locked(now_monotonic: float) -> None:
    """Drop records past their age limit, then the oldest beyond the size bound."""
    for key, (stored_at, response) in list(_recent_deploys.items()):
        if now_monotonic - stored_at > _record_ttl(response):
            _recent_deploys.pop(key, None)
    overflow = len(_recent_deploys) - MAX_PERSISTED_DEPLOYS
    if overflow > 0:
        oldest_first = sorted(_recent_deploys.items(), key=lambda item: item[1][0])
        for key, _item in oldest_first[:overflow]:
            _recent_deploys.pop(key, None)


def _snapshot_locked() -> dict:
    now_monotonic, now_wall = time.monotonic(), time.time()
    _prune_recent_locked(now_monotonic)
    return {
        "version": DEPLOY_STATE_VERSION,
        "saved_at": now_wall,
        "in_flight": [
            {
                "key": _key_to_json(key),
                "started_at": _in_flight_meta.get(key, (now_wall, "unknown"))[0],
                "triggered_by": _in_flight_meta.get(key, (now_wall, "unknown"))[1],
            }
            for key in sorted(_in_flight_deploys)
        ],
        "recent": [
            {
                "key": _key_to_json(key),
                "completed_at": now_wall - (now_monotonic - stored_at),
                "response": response,
            }
            for key, (stored_at, response) in sorted(
                _recent_deploys.items(), key=lambda item: item[1][0]
            )
        ],
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write `payload` so that `path` holds the old complete file or the new one."""
    # parents=False: a missing /workspace is a misconfiguration to report, not to create.
    path.parent.mkdir(exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _persist_deploy_state_locked() -> None:
    """Save the records. The caller holds `_deploy_state_lock`.

    A failed save never fails a deploy: the records stay in memory as before this change.
    The failure is logged and shown by /health (`deploy_state`), so lost durability is
    visible and not a silent green."""
    global _deploy_state_error
    try:
        _atomic_write_json(DEPLOY_STATE_FILE, _snapshot_locked())
        _deploy_state_error = ""
    except (OSError, TypeError, ValueError) as exc:
        message = f"{type(exc).__name__}: {exc}"
        if message != _deploy_state_error:
            logger.error(
                "Deploy records are not persisted to %s: %s", DEPLOY_STATE_FILE, message
            )
        _deploy_state_error = message


def _deploy_state_health() -> dict:
    error = _deploy_state_error
    return {
        "persistent": not error,
        "path": str(DEPLOY_STATE_FILE),
        "last_error": error,
        "records": len(_recent_deploys),
    }


def _begin_deploy_locked(key: DeploymentKey, triggered_by: str = "unknown") -> None:
    """Mark `key` in flight and save it. The caller holds `_deploy_state_lock`."""
    _in_flight_deploys.add(key)
    _in_flight_meta[key] = (time.time(), triggered_by)
    _persist_deploy_state_locked()


def _finish_deploy_locked(key: DeploymentKey, response: dict | None) -> None:
    """Store the result, clear the in-flight mark and save, in one step under the lock."""
    if response is not None:
        _recent_deploys[key] = (time.monotonic(), response)
    _in_flight_deploys.discard(key)
    _in_flight_meta.pop(key, None)
    _persist_deploy_state_locked()


def _quarantine_state_file(reason: str) -> None:
    quarantine = DEPLOY_STATE_FILE.with_name(DEPLOY_STATE_FILE.name + ".corrupt")
    logger.error(
        "Deploy state file %s is unusable (%s); starting empty, keeping it as %s",
        DEPLOY_STATE_FILE,
        reason,
        quarantine,
    )
    with contextlib.suppress(OSError):
        os.replace(DEPLOY_STATE_FILE, quarantine)


def _load_deploy_state() -> None:
    """Restore the records an earlier process saved. Runs once at import, before any
    request thread exists. A deploy that was in flight is now an interrupted record."""
    try:
        raw = DEPLOY_STATE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.error("Deploy state file %s is unreadable: %s", DEPLOY_STATE_FILE, exc)
        return
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != DEPLOY_STATE_VERSION:
            raise ValueError("unknown state file version")
        saved_recent, saved_in_flight = data.get("recent"), data.get("in_flight")
        if not isinstance(saved_recent, list) or not isinstance(saved_in_flight, list):
            raise ValueError("recent and in_flight must be lists")
    except ValueError as exc:
        _quarantine_state_file(str(exc))
        return

    now_wall, now_monotonic = time.time(), time.monotonic()
    restored = interrupted = 0
    with _deploy_state_lock:
        for item in saved_recent:
            if not isinstance(item, dict):
                continue
            key = _key_from_json(item.get("key"))
            response, completed_at = item.get("response"), item.get("completed_at")
            if key is None or not isinstance(response, dict):
                continue
            if isinstance(completed_at, bool) or not isinstance(
                completed_at, (int, float)
            ):
                continue
            age = max(0.0, now_wall - float(completed_at))
            _recent_deploys[key] = (now_monotonic - age, response)
            restored += 1
        for item in saved_in_flight:
            key = _key_from_json(item.get("key") if isinstance(item, dict) else None)
            if key is None:
                continue
            triggered_by = str(item.get("triggered_by") or "unknown")
            # The thread that ran this deploy died with the process.
            _recent_deploys[key] = (
                now_monotonic,
                _interrupted_response(key, now_wall, triggered_by),
            )
            interrupted += 1
        _prune_recent_locked(now_monotonic)
    logger.info(
        "Restored %d deploy record(s) from %s; %d deploy(s) were in flight at the restart "
        "and now read as interrupted",
        restored,
        DEPLOY_STATE_FILE,
        interrupted,
    )


def _reusable_result(key: DeploymentKey) -> dict | None:
    """A remembered result worth serving INSTEAD of deploying again — a success only.

    The window exists to collapse duplicate requests for work already done. A failure is
    not such work: an operator's natural next move is to fix the cause outside the
    request's coordinates and retry the same coordinates, and serving the remembered
    failure returns the original verbatim — same deployment_id, same message — as if
    nothing had been fixed. v1.1.77's staging soak failed on a stale local tag in the
    runner's workspace; the tag was repaired on the runner and the rerun reported the
    identical failure from memory (2026-09-09). Polling a deployment's status still
    reports the failure (that is how deploy_v2 learns of it); only the "skip the work"
    shortcut is limited to successes.

    Declining to reuse is all this does — it never discards the entry (review on this
    PR). The synchronous path reads the result it just stored AFTER releasing the lock,
    so a concurrent request that removed the failure here would leave the original one
    answering "Deployment finished without a stored result" about its own deploy. The
    entry ages out on the TTL like every other.
    """
    response = _recent_result(key)
    if response is None or response.get("status") == "completed":
        return response
    return None


def _in_progress_response(
    env: str,
    ref: str,
    triggered_by: str = "unknown",
    duplicate: bool = False,
    services: list[str] | None = None,
    version_ref: str | None = None,
    action: str = "sync",
) -> dict:
    key = _deployment_key(env, ref, services, version_ref, action)
    response = {
        "status": "in_progress",
        "deployment_id": _deployment_id(key),
        "env": env,
        "ref": ref,
        "triggered_by": triggered_by,
        "requested_services": list(key[2]),
        "status_url": "/deploy/status",
    }
    if version_ref:
        response["version_ref"] = version_ref
    if action and action != "sync":
        response["action"] = action
    if duplicate:
        response["duplicate"] = True
    return response


def _completed_response(
    env: str,
    ref: str,
    triggered_by: str,
    result,
    services: list[str] | None = None,
    version_ref: str | None = None,
    action: str = "sync",
) -> dict:
    # Keyed exactly as the deployment was stored (review on #630): a version_ref that
    # took part in the key must take part in the id, or /deploy/status looks up a
    # deployment that was never stored under that id.
    key = _deployment_key(env, ref, services, version_ref, action)
    result_payload = (
        result.to_public_dict()
        if hasattr(result, "to_public_dict")
        else result.to_dict()
    )
    response = {
        "status": "completed" if result.success else "failed",
        "deployment_id": _deployment_id(key),
        "env": env,
        "ref": ref,
        "triggered_by": triggered_by,
        "requested_services": list(key[2]),
        "result": result_payload,
    }
    if version_ref:
        response["version_ref"] = version_ref
    if action and action != "sync":
        response["action"] = action
    return response


def _version_kwargs(version_ref: str | None) -> dict:
    """Only a set version_ref travels as a kwarg, so callers and fakes that predate
    truealpha#712 keep their exact positional shape."""
    return {"version_ref": version_ref} if version_ref else {}


def _deployment_target(version_ref: str | None, action: str | None = None):
    """The thread target: _run_deployment itself when no version_ref/action is set (the
    exact legacy shape), else the same function with those bound in."""
    kwargs = {**_version_kwargs(version_ref), **_action_kwargs(action)}
    if not kwargs:
        return _run_deployment
    return functools.partial(_run_deployment, **kwargs)


def _run_deployment(
    env: str,
    ref: str,
    triggered_by: str,
    services: list[str] | None = None,
    version_ref: str | None = None,
    action: str = "sync",
) -> None:
    key = _deployment_key(env, ref, services, version_ref, action)
    response: dict | None = None
    try:
        from sync_runner import sync_services_by_version

        extra = {**_version_kwargs(version_ref), **_action_kwargs(action)}
        result = (
            sync_services_by_version(env, ref, triggered_by, services, **extra)
            if extra
            else sync_services_by_version(env, ref, triggered_by, services)
        )
        response = _completed_response(
            env,
            ref,
            triggered_by,
            result,
            services,
            version_ref=version_ref,
            action=action,
        )
    except Exception as exc:
        logger.exception("Deployment failed before producing a sync result")
        response = _failed_response(key, env, ref, triggered_by, str(exc))
    except BaseException as exc:
        # SystemExit or KeyboardInterrupt also ends this run. Record it as a failure,
        # let the finally block discard the key, and then let the exception propagate.
        logger.exception("Deployment interrupted before producing a sync result")
        response = _failed_response(
            key,
            env,
            ref,
            triggered_by,
            f"Deployment interrupted by {type(exc).__name__} before it produced a result",
        )
        raise
    finally:
        # Every exit path stores its result and discards the key in one locked step. A
        # poll never sees the key gone before its result exists, and no key stays in
        # flight until a restart (the bootstrap idle wait reads this set, #666).
        with _deploy_state_lock:
            _finish_deploy_locked(key, response)


def _failed_response(
    key: DeploymentKey, env: str, ref: str, triggered_by: str, error: str
) -> dict:
    return {
        "status": "failed",
        "deployment_id": _deployment_id(key),
        "env": env,
        "ref": ref,
        "triggered_by": triggered_by,
        "error": error,
        "requested_services": list(key[2]),
    }


def _dependency_checks() -> dict[str, bool]:
    checks = {
        f"python:{name}": importlib.util.find_spec(module) is not None
        for name, module in REQUIRED_PYTHON_MODULES.items()
    }
    checks.update(
        {f"binary:{name}": shutil.which(name) is not None for name in REQUIRED_BINARIES}
    )
    return checks


def op_service_account_works() -> bool:
    """Functionally authenticate the 1Password SA token (`op whoami`), not merely check the
    env var is present. Fail-closed; throttled to OP_HEALTHCHECK_TTL_SECONDS so /health
    doesn't call the op API every cycle. The old bare-presence check reported healthy while a
    deleted SA had silently broken op-gated deploys (#284) — token SET, op dead, green."""
    if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip():
        return False
    # monotonic clock for the TTL interval (matches _recent_result) — wall-clock jumps must
    # not cause extra op calls or an unexpectedly long cache window.
    with _op_health_lock:
        now = time.monotonic()
        if now - float(_op_health_cache["at"]) < OP_HEALTHCHECK_TTL_SECONDS:
            return bool(_op_health_cache["ok"])
        ok = False
        try:
            result = subprocess.run(["op", "whoami"], capture_output=True, timeout=10)
            ok = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        _op_health_cache["ok"] = ok
        _op_health_cache["at"] = time.monotonic()
        return ok


def _file_has_bytes(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


@app.route("/health", methods=["GET"])
def health():
    checks = {
        "http": True,
        "vault_secrets": _file_has_bytes(SECRETS_FILE),
        "git_repo_url": bool(GIT_REPO_URL),
        "webhook_secret": bool(WEBHOOK_SECRET),
        "op_service_account_token": op_service_account_works(),
        # Fail closed when DOKPLOY_API_KEY is empty: its Dokploy compose env can be
        # wiped on redeploy, after which every deploy fails with a cryptic "No
        # GitHub provider found". Surfacing it here makes the container unhealthy
        # immediately instead of silently accepting deploys it cannot complete.
        "dokploy_api_key": bool(os.environ.get("DOKPLOY_API_KEY", "").strip()),
    }
    checks.update(_dependency_checks())

    repo_name = Path(GIT_REPO_URL).stem if GIT_REPO_URL else "unknown"
    workspace_path = WORKSPACE / repo_name
    checks["git_workspace"] = workspace_path.exists()

    all_healthy = all(checks.values())
    status_code = 200 if all_healthy else 503

    # The bootstrap self-update reads this count and waits for zero before it recreates
    # the container, because a recreate drops this in-memory state (#666). It stays out
    # of `checks`: a count of 0 is falsy and would make the runner report "degraded".
    with _deploy_state_lock:
        in_flight_deploys = len(_in_flight_deploys)

    # Like the count, this stays out of `checks`: a runner that cannot save its records
    # still deploys, so it is reported and not made unhealthy (#1439).
    return jsonify(
        {
            "status": "healthy" if all_healthy else "degraded",
            "checks": checks,
            "in_flight_deploys": in_flight_deploys,
            "deploy_state": _deploy_state_health(),
            "git_sha": os.environ.get("GIT_SHA", "unknown").strip() or "unknown",
        }
    ), status_code


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event", "")
    if event != "push":
        return jsonify({"status": "ignored", "event": event})

    payload = request.json

    ref = payload.get("ref", "")
    if ref != f"refs/heads/{GIT_BRANCH}":
        return jsonify({"status": "ignored", "reason": f"Not {GIT_BRANCH} branch"})

    # A merge is not a release, so this endpoint deploys nothing.
    #
    # The delivery model has exactly three ways a change reaches a running service
    # (docs/ssot/ops.pipeline.md §2-3): an app PR gets a preview stack; a release tag
    # auto-promotes staging through reconcile-iac-inputs.yml; production is an explicit,
    # human promotion. infra2 itself has no persistent preview environment — a PR's
    # deploy_v2 canary is its preview.
    #
    # Deploying staging from an untagged main HEAD, which this handler used to do, is a
    # fourth path that contradicts all three: it makes "merge" mean "deploy", it puts
    # staging on a commit no tag names, and it does so from a webhook nobody watches.
    # It was silently dead from at least 2026-07-20 to 2026-09-09 (#585) with no service
    # visibly out of date, which is the strongest evidence available that nothing needs
    # it. The hook stays live and authenticated because a webhook that 401s is a broken
    # thing regardless, and because /deploy still uses this server.
    return jsonify(
        {
            "status": "ignored",
            "reason": "a push to main is not a deploy trigger; staging follows a release tag",
            "commit": payload.get("after", "")[:8],
        }
    )


def _keys_for_legacy_status(env: str, ref: str) -> list[DeploymentKey]:
    """Find old-client candidates; more than one is ambiguous and must fail closed."""
    active_recent = {
        key for key in list(_recent_deploys) if _recent_result(key) is not None
    }
    keys = set(_in_flight_deploys) | active_recent
    return sorted(key for key in keys if key[:2] == (env, ref))


@app.route("/deploy", methods=["POST"])
def version_deploy():
    if not verify_iac_request():
        return jsonify({"error": "Invalid signature"}), 401

    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    env = payload.get("env", "staging")
    # Support both 'ref' (generic) and 'tag' (legacy/specific)
    ref = validate_deploy_ref(payload.get("ref") or payload.get("tag"))
    triggered_by = payload.get("triggered_by", "unknown")
    try:
        wait = parse_bool(payload.get("wait", False))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not ref:
        return jsonify({"error": "Ref must be an exact 40-character commit SHA"}), 400

    if env not in ("staging", "production"):
        return jsonify({"error": "Invalid env (must be staging or production)"}), 400

    services = payload.get("services")
    if env == "production":
        oidc_ok, oidc_err, oidc_claims = verify_github_oidc_token(
            request.headers.get("Authorization", "")
        )
        if not oidc_ok:
            return jsonify({"error": oidc_err}), 403
        actor = str(oidc_claims.get("actor") or "").strip()
        if actor and triggered_by in ("unknown", "deploy_v2"):
            triggered_by = f"github-oidc:{actor}"
    if services is not None:
        if not isinstance(services, list) or not all(
            isinstance(s, str) for s in services
        ):
            return jsonify({"error": "services must be a list of strings"}), 400
        if not services or any(not service.strip() for service in services):
            return jsonify(
                {"error": "services must be a non-empty list of non-empty strings"}
            ), 400

    try:
        version_ref = validate_version_ref(payload.get("version_ref"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    action = payload.get("action") or "sync"
    if action not in DEPLOY_ACTIONS:
        return jsonify({"error": f"action must be one of {list(DEPLOY_ACTIONS)}"}), 400
    logger.info(
        f"Deployment: {ref} to {env} by {triggered_by}"
        + (f" (version_ref {version_ref})" if version_ref else "")
        + (f" (action {action})" if action != "sync" else "")
    )

    if wait:
        key = _deployment_key(env, ref, services, version_ref, action)
        with _deploy_state_lock:
            recent = _reusable_result(key)
            if recent:
                return jsonify({**recent, "cached": True}), 200
            if key in _in_flight_deploys:
                return jsonify(
                    _in_progress_response(
                        env,
                        ref,
                        triggered_by,
                        True,
                        services,
                        version_ref=version_ref,
                        action=action,
                    )
                ), 202
            _begin_deploy_locked(key, triggered_by)

        _run_deployment(
            env,
            ref,
            triggered_by,
            services,
            **_version_kwargs(version_ref),
            **_action_kwargs(action),
        )
        response = _recent_result(key) or {
            "status": "failed",
            "env": env,
            "ref": ref,
            "triggered_by": triggered_by,
            "error": "Deployment finished without a stored result",
        }
        status_code = 200 if response.get("status") == "completed" else 500
        return jsonify(response), status_code

    key = _deployment_key(env, ref, services, version_ref, action)
    with _deploy_state_lock:
        recent = _reusable_result(key)
        if recent:
            return jsonify({**recent, "cached": True}), 200
        if key in _in_flight_deploys:
            return jsonify(
                _in_progress_response(
                    env,
                    ref,
                    triggered_by,
                    True,
                    services,
                    version_ref=version_ref,
                    action=action,
                )
            ), 202
        _begin_deploy_locked(key, triggered_by)

    if services is not None:
        thread = threading.Thread(
            target=_deployment_target(version_ref, action),
            args=(env, ref, triggered_by, services),
        )
    else:
        thread = threading.Thread(
            target=_deployment_target(version_ref, action),
            args=(env, ref, triggered_by),
        )
    thread.daemon = True
    thread.start()

    return jsonify(
        {
            **_in_progress_response(
                env,
                ref,
                triggered_by,
                services=services,
                version_ref=version_ref,
                action=action,
            ),
            "wait": False,
        }
    ), 202


@app.route("/deploy/status", methods=["POST"])
def deployment_status():
    if not verify_iac_request():
        return jsonify({"error": "Invalid signature"}), 401

    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    env = payload.get("env", "staging")
    ref = validate_deploy_ref(payload.get("ref") or payload.get("tag"))
    triggered_by = payload.get("triggered_by", "unknown")
    deployment_id = payload.get("deployment_id")
    if not ref:
        return jsonify({"error": "Ref must be an exact 40-character commit SHA"}), 400
    if env not in ("staging", "production"):
        return jsonify({"error": "Invalid env (must be staging or production)"}), 400

    services = payload.get("services")
    if services is not None:
        if not isinstance(services, list) or not all(
            isinstance(s, str) for s in services
        ):
            return jsonify({"error": "services must be a list of strings"}), 400
        if not services or any(not service.strip() for service in services):
            return jsonify(
                {"error": "services must be a non-empty list of non-empty strings"}
            ), 400
    if deployment_id is not None and (
        not isinstance(deployment_id, str)
        or not DEPLOYMENT_ID_RE.fullmatch(deployment_id)
    ):
        return jsonify(
            {"error": "deployment_id must be 16 lowercase hex characters"}
        ), 400

    try:
        version_ref = validate_version_ref(payload.get("version_ref"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    action = payload.get("action") or "sync"
    if action not in DEPLOY_ACTIONS:
        return jsonify({"error": f"action must be one of {list(DEPLOY_ACTIONS)}"}), 400
    with _deploy_state_lock:
        if services is not None:
            key = _deployment_key(env, ref, services, version_ref, action)
        else:
            candidates = _keys_for_legacy_status(env, ref)
            if len(candidates) > 1:
                return jsonify(
                    {
                        "error": "Ambiguous deployment; deployment_id and services are required",
                        "candidate_deployment_ids": [
                            _deployment_id(item) for item in candidates
                        ],
                    }
                ), 409
            key = (
                candidates[0]
                if candidates
                else _deployment_key(env, ref, action=action)
            )

        expected_id = _deployment_id(key)
        if deployment_id is not None and deployment_id != expected_id:
            return jsonify(
                {
                    "error": "deployment_id does not match env/ref/services",
                    "expected_deployment_id": expected_id,
                }
            ), 409
        # A run in flight answers before any remembered result (2026-09-17). While a key
        # is in flight, a remembered result belongs to an EARLIER run: _run_deployment
        # stores its result and discards the key under this same lock. A failure stays
        # remembered for the TTL even after a retry starts (_reusable_result), so reading
        # it first handed the retry's poll the failure being retried (truealpha v0.0.83).
        if key in _in_flight_deploys:
            return jsonify(
                _in_progress_response(
                    env,
                    ref,
                    triggered_by,
                    services=list(key[2]),
                    version_ref=(key[3] if len(key) > 3 and key[3] else None),
                    action=(key[4] if len(key) > 4 and key[4] else "sync"),
                )
            ), 200
        recent = _recent_result(key)
        if recent:
            return jsonify(recent), 200

    return jsonify(
        {
            "status": "not_found",
            "deployment_id": _deployment_id(key),
            "env": env,
            "ref": ref,
        }
    ), 404


# Restore after every definition above exists. The save also writes back the interrupted
# records and proves at start-up that the state directory is writable.
_load_deploy_state()
with _deploy_state_lock:
    _persist_deploy_state_locked()


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 8080))
    app.run(host="0.0.0.0", port=port)
