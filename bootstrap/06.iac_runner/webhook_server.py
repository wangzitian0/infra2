#!/usr/bin/env python3
"""
GitHub Webhook Server for IaC Runner

Receives GitHub push events and triggers sync for changed services.
"""

import hashlib
import hmac
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WORKSPACE = Path("/workspace")
GIT_REPO_URL = os.environ.get("GIT_REPO_URL")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")
SECRETS_FILE = Path("/secrets/.env")
RECENT_DEPLOY_TTL_SECONDS = int(os.environ.get("RECENT_DEPLOY_TTL_SECONDS", "600"))
SIGNATURE_TTL_SECONDS = int(os.environ.get("SIGNATURE_TTL_SECONDS", "300"))
MAX_REQUEST_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", "65536"))
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
OP_HEALTHCHECK_TTL_SECONDS = int(os.environ.get("OP_HEALTHCHECK_TTL_SECONDS", "300"))
_op_health_lock = threading.Lock()
# `at` starts at -inf (always stale under monotonic time) so the FIRST /health after process
# start runs the real op check instead of serving the default ok=False — otherwise a fresh
# container whose monotonic clock is < TTL would report a false 503 for up to the TTL window.
_op_health_cache: dict[str, object] = {"ok": False, "at": float("-inf")}

_deploy_state_lock = threading.Lock()
DeploymentKey = tuple[str, str, tuple[str, ...]]
_in_flight_deploys: set[DeploymentKey] = set()
_recent_deploys: dict[DeploymentKey, tuple[float, dict]] = {}
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


def validate_deploy_ref(ref: str | None) -> str | None:
    if not isinstance(ref, str):
        return None
    normalized = ref.strip().lower()
    return normalized if EXACT_COMMIT_RE.fullmatch(normalized) else None


def get_changed_services(commits: list[dict]) -> set[str]:
    """Map a push's changed files to affected services via the deploy dependency
    graph — the SAME matcher used by the git-diff path, so both routes apply the
    manifest (own dir + declared deps) and neither uses a `libs/ -> __all__`
    catch-all. Importing sync_runner also puts the checked-out repo on sys.path
    so `libs.deploy_dependencies` resolves.
    """
    files = [
        file_path
        for commit in commits
        for file_path in (
            commit.get("added", [])
            + commit.get("modified", [])
            + commit.get("removed", [])
        )
    ]
    from sync_runner import get_changed_services_from_files

    return get_changed_services_from_files(files)


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


def run_sync(services: set[str]):
    from sync_runner import sync_services

    thread = threading.Thread(target=sync_services, args=(services,))
    thread.daemon = True
    thread.start()


def _normalize_services(services: list[str] | None) -> tuple[str, ...]:
    """Canonical service-set identity; omitted services means the all-services operation."""
    if services is None:
        return ("__all__",)
    return tuple(sorted({service.strip() for service in services}))


def _deployment_key(
    env: str, ref: str, services: list[str] | None = None
) -> DeploymentKey:
    return (env, ref, _normalize_services(services))


def _deployment_id(key: DeploymentKey) -> str:
    service_identity = ",".join(key[2])
    return hashlib.sha256(
        f"{key[0]}:{key[1]}:{service_identity}".encode()
    ).hexdigest()[:16]


def _recent_result(key: DeploymentKey) -> dict | None:
    item = _recent_deploys.get(key)
    if not item:
        return None
    completed_at, response = item
    if time.monotonic() - completed_at > RECENT_DEPLOY_TTL_SECONDS:
        _recent_deploys.pop(key, None)
        return None
    return response


def _in_progress_response(
    env: str,
    ref: str,
    triggered_by: str = "unknown",
    duplicate: bool = False,
    services: list[str] | None = None,
) -> dict:
    key = _deployment_key(env, ref, services)
    response = {
        "status": "in_progress",
        "deployment_id": _deployment_id(key),
        "env": env,
        "ref": ref,
        "triggered_by": triggered_by,
        "requested_services": list(key[2]),
        "status_url": "/deploy/status",
    }
    if duplicate:
        response["duplicate"] = True
    return response


def _completed_response(
    env: str,
    ref: str,
    triggered_by: str,
    result,
    services: list[str] | None = None,
) -> dict:
    key = _deployment_key(env, ref, services)
    result_payload = (
        result.to_public_dict() if hasattr(result, "to_public_dict") else result.to_dict()
    )
    return {
        "status": "completed" if result.success else "failed",
        "deployment_id": _deployment_id(key),
        "env": env,
        "ref": ref,
        "triggered_by": triggered_by,
        "requested_services": list(key[2]),
        "result": result_payload,
    }


def _run_deployment(env: str, ref: str, triggered_by: str, services: list[str] | None = None) -> None:
    from sync_runner import sync_services_by_version

    key = _deployment_key(env, ref, services)
    try:
        result = sync_services_by_version(env, ref, triggered_by, services)
        response = _completed_response(env, ref, triggered_by, result, services)
    except Exception as exc:
        logger.exception("Deployment failed before producing a sync result")
        response = {
            "status": "failed",
            "deployment_id": _deployment_id(key),
            "env": env,
            "ref": ref,
            "triggered_by": triggered_by,
            "error": str(exc),
            "requested_services": list(key[2]),
        }
    with _deploy_state_lock:
        _recent_deploys[key] = (time.monotonic(), response)
        _in_flight_deploys.discard(key)


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
    now = time.monotonic()
    with _op_health_lock:
        if now - float(_op_health_cache["at"]) < OP_HEALTHCHECK_TTL_SECONDS:
            return bool(_op_health_cache["ok"])
    ok = False
    try:
        result = subprocess.run(
            ["op", "whoami"], capture_output=True, timeout=10
        )
        ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    with _op_health_lock:
        _op_health_cache["ok"] = ok
        _op_health_cache["at"] = now
    return ok


@app.route("/health", methods=["GET"])
def health():
    checks = {
        "http": True,
        "vault_secrets": SECRETS_FILE.exists() and SECRETS_FILE.stat().st_size > 0,
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

    return jsonify(
        {"status": "healthy" if all_healthy else "degraded", "checks": checks}
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

    commits = payload.get("commits", [])
    services = get_changed_services(commits)

    if not services:
        return jsonify({"status": "no_changes", "message": "No service files changed"})

    run_sync(services)

    return jsonify(
        {
            "status": "accepted",
            "services": list(services),
            "commit": payload.get("after", "")[:8],
        }
    )


@app.route("/sync", methods=["POST"])
def manual_sync():
    if os.environ.get("ENABLE_LEGACY_SYNC", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return jsonify({"error": "Legacy sync endpoint disabled"}), 404
    if not verify_iac_request():
        return jsonify({"error": "Invalid signature"}), 401

    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400

    if payload.get("all"):
        services = {"__all__"}
    else:
        services = set(payload.get("services", []))

    if not services:
        return jsonify({"error": "No services specified"}), 400

    run_sync(services)

    return jsonify({"status": "accepted", "services": list(services)})


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
    if services is not None:
        if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
            return jsonify({"error": "services must be a list of strings"}), 400
        if not services or any(not service.strip() for service in services):
            return jsonify({"error": "services must be a non-empty list of non-empty strings"}), 400

    logger.info(f"Deployment: {ref} to {env} by {triggered_by}")

    if wait:
        key = _deployment_key(env, ref, services)
        with _deploy_state_lock:
            recent = _recent_result(key)
            if recent:
                status_code = 200 if recent.get("status") == "completed" else 500
                return jsonify({**recent, "cached": True}), status_code
            if key in _in_flight_deploys:
                return jsonify(
                    _in_progress_response(env, ref, triggered_by, True, services)
                ), 202
            _in_flight_deploys.add(key)

        _run_deployment(env, ref, triggered_by, services)
        response = _recent_result(key) or {
            "status": "failed",
            "env": env,
            "ref": ref,
            "triggered_by": triggered_by,
            "error": "Deployment finished without a stored result",
        }
        status_code = 200 if response.get("status") == "completed" else 500
        return jsonify(response), status_code

    key = _deployment_key(env, ref, services)
    with _deploy_state_lock:
        recent = _recent_result(key)
        if recent:
            return jsonify({**recent, "cached": True}), 200
        if key in _in_flight_deploys:
            return jsonify(
                _in_progress_response(env, ref, triggered_by, True, services)
            ), 202
        _in_flight_deploys.add(key)

    if services is not None:
        thread = threading.Thread(target=_run_deployment, args=(env, ref, triggered_by, services))
    else:
        thread = threading.Thread(target=_run_deployment, args=(env, ref, triggered_by))
    thread.daemon = True
    thread.start()

    return jsonify(
        {**_in_progress_response(env, ref, triggered_by, services=services), "wait": False}
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
        if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
            return jsonify({"error": "services must be a list of strings"}), 400
        if not services or any(not service.strip() for service in services):
            return jsonify({"error": "services must be a non-empty list of non-empty strings"}), 400
    if deployment_id is not None and (
        not isinstance(deployment_id, str) or not DEPLOYMENT_ID_RE.fullmatch(deployment_id)
    ):
        return jsonify({"error": "deployment_id must be 16 lowercase hex characters"}), 400

    with _deploy_state_lock:
        if services is not None:
            key = _deployment_key(env, ref, services)
        else:
            candidates = _keys_for_legacy_status(env, ref)
            if len(candidates) > 1:
                return jsonify(
                    {
                        "error": "Ambiguous deployment; deployment_id and services are required",
                        "candidate_deployment_ids": [_deployment_id(item) for item in candidates],
                    }
                ), 409
            key = candidates[0] if candidates else _deployment_key(env, ref)

        expected_id = _deployment_id(key)
        if deployment_id is not None and deployment_id != expected_id:
            return jsonify(
                {
                    "error": "deployment_id does not match env/ref/services",
                    "expected_deployment_id": expected_id,
                }
            ), 409
        recent = _recent_result(key)
        if recent:
            return jsonify(recent), 200
        if key in _in_flight_deploys:
            return jsonify(
                _in_progress_response(
                    env, ref, triggered_by, services=list(key[2])
                )
            ), 200

    return jsonify(
        {
            "status": "not_found",
            "deployment_id": _deployment_id(key),
            "env": env,
            "ref": ref,
        }
    ), 404


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 8080))
    app.run(host="0.0.0.0", port=port)
