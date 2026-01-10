#!/usr/bin/env python3
"""
GitHub Webhook Server for IaC Runner

Receives GitHub push events and triggers sync for changed services.
"""
import hashlib
import hmac
import json
import os
import subprocess
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WORKSPACE = "/workspace"
GIT_REPO_URL = os.environ.get("GIT_REPO_URL", "https://github.com/wangzitian0/infra2.git")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub webhook signature."""
    if not WEBHOOK_SECRET:
        return True  # No secret configured, skip verification (dev mode)
    
    if not signature or not signature.startswith("sha256="):
        return False
    
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected, signature)


def get_changed_services(commits: list[dict]) -> set[str]:
    """Extract changed service paths from commits.
    
    Returns set of service identifiers like:
    - platform/postgres
    - finance_report/app
    - bootstrap/vault
    """
    services = set()
    
    for commit in commits:
        for file_path in commit.get("added", []) + commit.get("modified", []) + commit.get("removed", []):
            parts = file_path.split("/")
            
            # Pattern: platform/{nn}.{service}/* -> platform/{service}
            if parts[0] == "platform" and len(parts) >= 2:
                # Extract service name from "{nn}.{service}"
                service_dir = parts[1]
                if "." in service_dir:
                    service = service_dir.split(".", 1)[1]
                    services.add(f"platform/{service}")
            
            # Pattern: finance_report/finance_report/{nn}.{service}/* -> finance_report/{service}
            elif parts[0] == "finance_report" and len(parts) >= 3:
                service_dir = parts[2]
                if "." in service_dir:
                    service = service_dir.split(".", 1)[1]
                    services.add(f"finance_report/{service}")
            
            # Pattern: bootstrap/{nn}.{service}/* -> bootstrap/{service}
            elif parts[0] == "bootstrap" and len(parts) >= 2:
                service_dir = parts[1]
                if "." in service_dir:
                    service = service_dir.split(".", 1)[1]
                    services.add(f"bootstrap/{service}")
            
            # Pattern: libs/* -> sync all
            elif parts[0] == "libs":
                services.add("__all__")
    
    return services


def run_sync(services: set[str]):
    """Run sync for changed services in background."""
    from sync_runner import sync_services
    
    thread = threading.Thread(target=sync_services, args=(services,))
    thread.daemon = True
    thread.start()


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """GitHub webhook endpoint."""
    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        return jsonify({"error": "Invalid signature"}), 401
    
    # Check event type
    event = request.headers.get("X-GitHub-Event", "")
    if event != "push":
        return jsonify({"status": "ignored", "event": event})
    
    payload = request.json
    
    # Only process pushes to main branch
    ref = payload.get("ref", "")
    if ref != f"refs/heads/{GIT_BRANCH}":
        return jsonify({"status": "ignored", "reason": f"Not {GIT_BRANCH} branch"})
    
    # Get changed services
    commits = payload.get("commits", [])
    services = get_changed_services(commits)
    
    if not services:
        return jsonify({"status": "no_changes", "message": "No service files changed"})
    
    # Trigger sync in background
    run_sync(services)
    
    return jsonify({
        "status": "accepted",
        "services": list(services),
        "commit": payload.get("after", "")[:8]
    })


@app.route("/sync", methods=["POST"])
def manual_sync():
    """Manual sync trigger endpoint.
    
    Body: {"services": ["platform/postgres", "finance_report/app"]}
    Or: {"all": true} to sync everything
    """
    # Verify signature for manual triggers too
    signature = request.headers.get("X-Hub-Signature-256", "")
    if WEBHOOK_SECRET and not verify_signature(request.data, signature):
        return jsonify({"error": "Invalid signature"}), 401
    
    payload = request.json or {}
    
    if payload.get("all"):
        services = {"__all__"}
    else:
        services = set(payload.get("services", []))
    
    if not services:
        return jsonify({"error": "No services specified"}), 400
    
    run_sync(services)
    
    return jsonify({
        "status": "accepted",
        "services": list(services)
    })


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 8080))
    app.run(host="0.0.0.0", port=port)
