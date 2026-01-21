#!/usr/bin/env python3
"""
GitHub Webhook Server for IaC Runner

Receives GitHub push events and triggers sync for changed services.
"""

import hashlib
import hmac
import logging
import os
import threading
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

if not GIT_REPO_URL:
    raise RuntimeError("GIT_REPO_URL environment variable must be set")


def verify_signature(payload: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not configured - running in insecure dev mode!")
        return True

    if not signature or not signature.startswith("sha256="):
        return False

    expected = (
        "sha256="
        + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    )

    return hmac.compare_digest(expected, signature)


def get_changed_services(commits: list[dict]) -> set[str]:
    services = set()

    for commit in commits:
        for file_path in (
            commit.get("added", [])
            + commit.get("modified", [])
            + commit.get("removed", [])
        ):
            parts = file_path.split("/")

            if parts[0] == "platform" and len(parts) >= 2:
                service_dir = parts[1]
                if "." in service_dir:
                    service = service_dir.split(".", 1)[1]
                    services.add(f"platform/{service}")

            elif parts[0] == "finance_report" and len(parts) >= 3:
                service_dir = parts[2]
                if "." in service_dir:
                    service = service_dir.split(".", 1)[1]
                    services.add(f"finance_report/{service}")

            elif parts[0] == "bootstrap" and len(parts) >= 2:
                service_dir = parts[1]
                if "." in service_dir:
                    service = service_dir.split(".", 1)[1]
                    services.add(f"bootstrap/{service}")

            elif parts[0] == "libs":
                services.add("__all__")

    return services


def run_sync(services: set[str]):
    from sync_runner import sync_services

    thread = threading.Thread(target=sync_services, args=(services,))
    thread.daemon = True
    thread.start()


@app.route("/health", methods=["GET"])
def health():
    checks = {
        "http": True,
        "vault_secrets": SECRETS_FILE.exists() and SECRETS_FILE.stat().st_size > 0,
        "git_repo_url": bool(GIT_REPO_URL),
        "webhook_secret": bool(WEBHOOK_SECRET),
    }

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
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        return jsonify({"error": "Invalid signature"}), 401

    payload = request.json or {}

    if payload.get("all"):
        services = {"__all__"}
    else:
        services = set(payload.get("services", []))

    if not services:
        return jsonify({"error": "No services specified"}), 400

    run_sync(services)

    return jsonify({"status": "accepted", "services": list(services)})


@app.route("/deploy", methods=["POST"])
def version_deploy():
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        return jsonify({"error": "Invalid signature"}), 401

    payload = request.json or {}
    env = payload.get("env", "staging")
    # Support both 'ref' (generic) and 'tag' (legacy/specific)
    ref = payload.get("ref") or payload.get("tag")
    triggered_by = payload.get("triggered_by", "unknown")

    if not ref:
        return jsonify({"error": "Ref or Tag required"}), 400

    if env not in ("staging", "production"):
        return jsonify({"error": "Invalid env (must be staging or production)"}), 400

    logger.info(f"Deployment: {ref} to {env} by {triggered_by}")

    from sync_runner import sync_services_by_version

    thread = threading.Thread(
        target=sync_services_by_version, args=(env, ref, triggered_by)
    )
    thread.daemon = True
    thread.start()

    return jsonify(
        {"status": "accepted", "env": env, "ref": ref, "triggered_by": triggered_by}
    )


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 8080))
    app.run(host="0.0.0.0", port=port)
