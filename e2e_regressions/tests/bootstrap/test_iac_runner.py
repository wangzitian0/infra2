"""
E2E Tests for IaC Runner (GitOps Webhook Service)
"""

import hashlib
import hmac
import os

import httpx
import pytest


@pytest.fixture
def iac_runner_url():
    """Get IaC runner URL from environment."""
    internal_domain = os.environ.get("INTERNAL_DOMAIN", "zitian.party")
    return f"https://iac.{internal_domain}"


@pytest.fixture
def webhook_secret():
    """Get webhook secret from environment or skip test."""
    secret = os.environ.get("IAC_RUNNER_WEBHOOK_SECRET")
    if not secret:
        pytest.skip("IAC_RUNNER_WEBHOOK_SECRET not set")
    return secret


def compute_signature(payload: bytes, secret: str) -> str:
    """Compute GitHub webhook signature."""
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_health_endpoint(iac_runner_url):
    """Test IaC runner health check."""
    response = httpx.get(f"{iac_runner_url}/health", timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_webhook_signature_validation(iac_runner_url, webhook_secret):
    """Test webhook signature verification."""
    payload = b'{"ref": "refs/heads/main", "commits": []}'
    signature = compute_signature(payload, webhook_secret)

    response = httpx.post(
        f"{iac_runner_url}/webhook",
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
        content=payload,
        timeout=10,
    )

    assert response.status_code == 200
    data = response.json()
    assert "status" in data


def test_webhook_invalid_signature(iac_runner_url):
    """Test webhook rejects invalid signature."""
    payload = b'{"ref": "refs/heads/main", "commits": []}'

    response = httpx.post(
        f"{iac_runner_url}/webhook",
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
        content=payload,
        timeout=10,
    )

    assert response.status_code == 401


def test_webhook_no_changes(iac_runner_url, webhook_secret):
    """Test webhook response when no service files changed."""
    payload = b"""{
        "ref": "refs/heads/main",
        "commits": [{
            "added": ["README.md"],
            "modified": [],
            "removed": []
        }]
    }"""
    signature = compute_signature(payload, webhook_secret)

    response = httpx.post(
        f"{iac_runner_url}/webhook",
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
        content=payload,
        timeout=10,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "no_changes"


def test_webhook_platform_service_change(iac_runner_url, webhook_secret):
    """Test webhook detects platform service changes."""
    payload = b"""{
        "ref": "refs/heads/main",
        "after": "abc123def456",
        "commits": [{
            "added": [],
            "modified": ["platform/01.postgres/compose.yaml"],
            "removed": []
        }]
    }"""
    signature = compute_signature(payload, webhook_secret)

    response = httpx.post(
        f"{iac_runner_url}/webhook",
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
        content=payload,
        timeout=10,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "platform/postgres" in data["services"]


def test_webhook_libs_change_triggers_all(iac_runner_url, webhook_secret):
    """Test that libs/ changes trigger sync for all services."""
    payload = b"""{
        "ref": "refs/heads/main",
        "after": "abc123def456",
        "commits": [{
            "added": [],
            "modified": ["libs/deployer.py"],
            "removed": []
        }]
    }"""
    signature = compute_signature(payload, webhook_secret)

    response = httpx.post(
        f"{iac_runner_url}/webhook",
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
        content=payload,
        timeout=10,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "__all__" in data["services"]


@pytest.mark.slow
def test_manual_sync_endpoint(iac_runner_url, webhook_secret):
    """Test manual sync trigger endpoint."""
    payload = b'{"services": ["platform/postgres"]}'
    signature = compute_signature(payload, webhook_secret)

    response = httpx.post(
        f"{iac_runner_url}/sync",
        headers={
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
        content=payload,
        timeout=30,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "platform/postgres" in data["services"]
