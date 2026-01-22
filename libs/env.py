"""
Simplified environment and secret management

Three credential types:
- bootstrap: 1Password - all bootstrap project credentials
- root_vars: 1Password - superadmin passwords for non-bootstrap services
- app_vars: Vault - application variables (consumed by vault-agent)

Two backends:
- OpSecrets: 1Password (uses OP_SERVICE_ACCOUNT_TOKEN)
- VaultSecrets: HashiCorp Vault (uses VAULT_ROOT_TOKEN)
"""

from __future__ import annotations
import os
import json
import secrets
import string
import subprocess
import sys
from typing import Literal, Optional

import httpx


CredentialType = Literal["bootstrap", "root_vars", "app_vars"]

__all__ = [
    "OpSecrets",
    "VaultSecrets",
    "get_secrets",
    "generate_password",
    "CredentialType",
]


def generate_password(length: int = 24) -> str:
    """Generate secure random alphanumeric password"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _validate_scope_value(
    label: str, value: str | None, allow_none: bool = False
) -> str | None:
    """Validate project/env/service values for consistent path mapping."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{label} is required")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{label} must not be empty")
    if "-" in trimmed or "/" in trimmed:
        raise ValueError(f"{label} must not include '-' or '/'")
    return trimmed


class OpSecrets:
    """1Password secrets for bootstrap phase.

    Requires OP_SERVICE_ACCOUNT_TOKEN environment variable.
    """

    VAULT = "Infra2"
    INIT_ITEM = "init/env_vars"

    def __init__(self, item: str = INIT_ITEM):
        self.item = item
        self._cache: dict | None = None

    def _load(self) -> dict[str, str]:
        """Load all fields from 1Password item"""
        if self._cache is not None:
            return self._cache

        try:
            result = subprocess.run(
                [
                    "op",
                    "item",
                    "get",
                    self.item,
                    f"--vault={self.VAULT}",
                    "--format=json",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            item = json.loads(result.stdout)
            self._cache = {
                f["label"]: f.get("value", "")
                for f in item.get("fields", [])
                if f.get("label")
                and f.get("label") not in ["notesPlain", "password", "username"]
            }
        except FileNotFoundError:
            self._cache = {}
        except subprocess.CalledProcessError as e:
            print(f"OpSecrets: failed to load {self.item}: {e}", file=sys.stderr)
            self._cache = {}
        except json.JSONDecodeError as e:
            print(f"OpSecrets: invalid JSON from {self.item}: {e}", file=sys.stderr)
            self._cache = {}
        return self._cache

    def get(self, key: str) -> Optional[str]:
        """Get a single field value"""
        return self._load().get(key)

    def get_all(self) -> dict[str, str]:
        """Get all fields"""
        return self._load()

    def set(self, key: str, value: str) -> bool:
        """Set a field value"""
        try:
            subprocess.run(
                [
                    "op",
                    "item",
                    "edit",
                    self.item,
                    f"--vault={self.VAULT}",
                    f"{key}={value}",
                ],
                capture_output=True,
                check=True,
            )
            self._cache = None  # Invalidate cache
            return True
        except subprocess.CalledProcessError as e:
            print(f"OpSecrets: failed to set {key}: {e}", file=sys.stderr)
            return False


class VaultSecrets:
    """Vault secrets for platform services.

    Uses HTTP API directly (no vault CLI dependency).
    Set VAULT_SKIP_VERIFY=1 to skip SSL verification for self-signed certs.
    """

    def __init__(self, path: str, token: str | None = None, addr: str | None = None):
        """
        Args:
            path: Secret path (e.g., "platform/production/postgres")
            token: Vault token (default: from VAULT_ROOT_TOKEN env)
            addr: Vault address (default: from VAULT_ADDR or INTERNAL_DOMAIN)
        """
        self.path = path
        self.token = token or os.getenv("VAULT_ROOT_TOKEN")
        self.addr = addr or self._get_addr()
        self.verify_ssl = os.getenv("VAULT_SKIP_VERIFY", "").lower() not in (
            "1",
            "true",
            "yes",
        )
        self._cache: dict | None = None

    @staticmethod
    def _get_addr() -> str:
        """Get Vault address from environment only (no 1Password dependency)"""
        if addr := os.getenv("VAULT_ADDR"):
            return addr
        if domain := os.getenv("INTERNAL_DOMAIN"):
            return f"https://vault.{domain}"
        return "https://vault.localhost"

    def _load(self) -> dict[str, str]:
        """Load secrets from Vault"""
        if self._cache is not None:
            return self._cache

        if not self.token:
            print("\n❌ Error: VAULT_ROOT_TOKEN not set", file=sys.stderr)
            print("Please set: export VAULT_ROOT_TOKEN=<admin-token>", file=sys.stderr)
            print(
                "Get from: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' "
                "(or /Token; item: bootstrap/vault/Root Token)",
                file=sys.stderr,
            )
            self._cache = {}
            return self._cache

        try:
            with httpx.Client(verify=self.verify_ssl, timeout=10.0) as client:
                resp = client.get(
                    f"{self.addr}/v1/secret/data/{self.path}",
                    headers={"X-Vault-Token": self.token},
                )
                if resp.status_code == 200:
                    self._cache = resp.json().get("data", {}).get("data", {})
                else:
                    self._cache = {}
        except httpx.RequestError as e:
            print(
                f"VaultSecrets: connection error to {self.addr}: {e}", file=sys.stderr
            )
            self._cache = {}
        except Exception as e:
            print(f"VaultSecrets: unexpected error: {e}", file=sys.stderr)
            self._cache = {}
        return self._cache

    def get(self, key: str) -> Optional[str]:
        """Get a single secret"""
        return self._load().get(key)

    def get_all(self) -> dict[str, str]:
        """Get all secrets"""
        return self._load()

    def set(self, key: str, value: str) -> bool:
        """Set a secret (merge with existing)"""
        if not self.token:
            return False

        existing = self._load().copy()
        existing[key] = value

        try:
            with httpx.Client(verify=self.verify_ssl, timeout=10.0) as client:
                resp = client.post(
                    f"{self.addr}/v1/secret/data/{self.path}",
                    headers={"X-Vault-Token": self.token},
                    json={"data": existing},
                )
                if resp.status_code in (200, 204):
                    self._cache = None  # Invalidate
                    return True
        except httpx.RequestError as e:
            print(f"VaultSecrets: connection error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"VaultSecrets: unexpected error: {e}", file=sys.stderr)
        return False


def get_secrets(
    project: str,
    service: str | None = None,
    env: str = "production",
    credential_type: CredentialType | None = None,
) -> OpSecrets | VaultSecrets:
    """Factory to get appropriate secrets backend based on credential type.

    Args:
        project: Project name (e.g., 'bootstrap', 'platform', 'finance_report')
        service: Service name (e.g., 'postgres'), None uses project/env path
        env: Environment (default: 'production')
        credential_type: Credential type - 'bootstrap', 'root_vars', or 'app_vars'
                        If not specified, defaults to 'app_vars' (Vault)

    Returns:
        OpSecrets (1Password) for bootstrap/root_vars
        VaultSecrets for app_vars

    Type routing:
        bootstrap  -> 1Password: {project}/{service} (no env layer)
        root_vars  -> 1Password: {project}/{env}/{service}
        app_vars   -> Vault: secret/data/{project}/{env}/{service}
    """
    validated_project: str = _validate_scope_value("project", project)  # type: ignore[assignment]
    validated_env: str = _validate_scope_value("env", env)  # type: ignore[assignment]
    validated_service = _validate_scope_value("service", service, allow_none=True)

    resolved_type = credential_type or "app_vars"

    if resolved_type == "bootstrap":
        item = (
            f"{validated_project}/{validated_service}"
            if validated_service
            else validated_project
        )
        return OpSecrets(item=item)
    elif resolved_type == "root_vars":
        item = (
            f"{validated_project}/{validated_env}/{validated_service}"
            if validated_service
            else f"{validated_project}/{validated_env}"
        )
        return OpSecrets(item=item)
    else:
        path = (
            f"{validated_project}/{validated_env}/{validated_service}"
            if validated_service
            else f"{validated_project}/{validated_env}"
        )
        return VaultSecrets(path=path)
