"""Deployment preflight guards and credential checks.

Consolidates duplicate token and AppRole verification from libs.deploy.deployer
and libs.deploy.promote onto infra2_sdk.secrets.
"""

from __future__ import annotations

from typing import Any
from infra2_sdk.secrets import vault_token_status


def parse_env_var(env_text: str | None, key: str) -> str | None:
    """Read one KEY=VALUE from a Dokploy compose env string.

    Returns None if absent or empty. Comments starting with '#' are ignored.
    """
    for line in (env_text or "").splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            val = line.split("=", 1)[1].strip()
            return val if val else None
    return None


def extract_vault_token_from_env(env_text: str | None) -> tuple[bool, str | None]:
    """Parse Dokploy compose env string for legacy VAULT_APP_TOKEN.

    Returns:
        (is_approle, token_or_none)
    """
    if not env_text:
        return False, None

    # AppRole services authenticate via VAULT_ROLE_ID/VAULT_SECRET_ID.
    # A vestigial VAULT_APP_TOKEN left in Dokploy is unused and would expire
    # un-renewed, so gating on it would hard-block an AppRole deploy.
    if parse_env_var(env_text, "VAULT_ROLE_ID") and parse_env_var(
        env_text, "VAULT_SECRET_ID"
    ):
        return True, None

    token = parse_env_var(env_text, "VAULT_APP_TOKEN")
    return False, token


def check_approle_creds(compose_text: str, env_text: str | None) -> list[str]:
    """Return missing AppRole credentials if the compose uses AppRole auth."""
    if "VAULT_ROLE_ID" not in compose_text and "VAULT_SECRET_ID" not in compose_text:
        return []

    return [
        key
        for key in ("VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_ADDR")
        if not parse_env_var(env_text, key)
    ]


def verify_token_status(
    token: str,
    vault_addr: str,
    *,
    min_ttl_hours: int = 24,
    verifier: Any = None,
) -> dict:
    """Verify a Vault token via infra2-sdk vault_token_status.

    Returns the contract dict expected by Deployer and promote:
    valid, ttl_hours, renewable, error, details.
    """
    if verifier is not None:
        res = verifier(token, addr=vault_addr, min_ttl_hours=min_ttl_hours)
        if isinstance(res, dict):
            return res
        return {
            "valid": bool(res),
            "ttl_hours": getattr(res, "ttl_hours", -1),
            "renewable": bool(getattr(res, "renewable", False)),
            "error": getattr(res, "error", None),
            "details": getattr(res, "details", ""),
        }

    status = vault_token_status(vault_addr, token, min_ttl_seconds=min_ttl_hours * 3600)
    is_valid = bool(status.valid)
    ttl_h = status.ttl_hours if status.ttl_seconds >= 0 else -1
    err = None if is_valid else (status.error or "invalid token")
    details = f"Token OK (TTL: {ttl_h}h)" if is_valid else f"Token invalid: {err}"
    return {
        "valid": is_valid,
        "ttl_hours": ttl_h,
        "renewable": bool(status.renewable),
        "error": err,
        "details": details,
    }
