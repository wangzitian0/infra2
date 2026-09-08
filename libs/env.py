"""Secret-store access for infra2 tasks, over the infra2-sdk adapters (plan PR-E).

Three credential types, two stores:

- bootstrap: 1Password ``{project}/{service}`` — provisioning credentials, no env layer
- root_vars: 1Password ``{project}/{env}/{service}`` — human-entered values per env
- app_vars:  Vault ``secret/{project}/{env}/{service}`` — what services read at runtime

The Vault token comes from ``VAULT_TOKEN`` (the runner's bounded AppRole token, or a
break-glass token minted by ``bootstrap/05.vault``); ``VAULT_ROOT_TOKEN`` is accepted as a
transition alias only. No task reads the root token from 1Password.
"""

from __future__ import annotations

import os
import secrets as _secrets
import string
import sys
from typing import Literal, Optional

import httpx

try:
    from infra2_sdk.secrets import OnePasswordBackend, SecretsError, VaultKvBackend
except ModuleNotFoundError:  # pragma: no cover - environment, not logic
    # Helpers (generate_password, vault_token, verify_vault_token) stay importable in
    # minimal environments; only constructing a store without an injected backend
    # needs the SDK, and says so.
    OnePasswordBackend = VaultKvBackend = None  # type: ignore[assignment,misc]

    class SecretsError(RuntimeError):  # type: ignore[no-redef]
        pass


_SDK_MISSING = (
    "infra2-sdk is required to read or write secret stores: it is pinned in pyproject.toml "
    "and in bootstrap/06.iac_runner/requirements.txt; install the project dependencies "
    "(uv sync) or rebuild the iac-runner image (deploy.yml does so on the next main push)."
)


def _require_sdk(cls):
    if cls is None:
        raise ModuleNotFoundError(_SDK_MISSING)
    return cls


CredentialType = Literal["bootstrap", "root_vars", "app_vars"]

# The bootstrap root token's 1Password reference: only bootstrap/05.vault reads it.
VAULT_ROOT_TOKEN_OP_REF = "op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token"

_SCOPE_ALLOWED = set(string.ascii_lowercase + string.digits + "_")


def generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(_secrets.choice(alphabet) for _ in range(length))


def _validate_scope_value(
    name: str, value: str | None, *, allow_none: bool = False
) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} is required")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if any(ch not in _SCOPE_ALLOWED for ch in value):
        raise ValueError(
            f"{name} must not include characters outside [a-z0-9_] (got {value!r}); "
            "Vault paths and 1Password items use underscores"
        )
    return value


def vault_token(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    return env.get("VAULT_TOKEN") or env.get("VAULT_ROOT_TOKEN") or None


def vault_address(environ=None) -> str:
    env = os.environ if environ is None else environ
    if addr := env.get("VAULT_ADDR"):
        return addr
    if domain := env.get("INTERNAL_DOMAIN"):
        return f"https://vault.{domain}"
    return "https://vault.localhost"


class OpSecrets:
    """One 1Password item, read through the SDK's ``op`` adapter."""

    VAULT = "Infra2"
    INIT_ITEM = "init/env_vars"

    def __init__(
        self, item: str = INIT_ITEM, backend: OnePasswordBackend | None = None
    ):
        self.item = item
        self._backend = backend  # built on first use so importing tasks needs no SDK
        self._cache: dict[str, str] | None = None

    def _client(self) -> OnePasswordBackend:
        if self._backend is None:
            self._backend = _require_sdk(OnePasswordBackend)(self.VAULT)
        return self._backend

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            try:
                raw = self._client().read(self.item)
            except (SecretsError, OSError) as error:
                # OSError: no `op` binary (CI runners, GitHub Actions) — degrade to an
                # empty read exactly as the pre-SDK implementation did.
                print(
                    f"OpSecrets: failed to load {self.item}: {error}", file=sys.stderr
                )
                raw = {}
            self._cache = {
                k: v
                for k, v in raw.items()
                if k not in ("notesPlain", "password", "username")
            }
        return self._cache

    def get(self, key: str) -> Optional[str]:
        return self._load().get(key)

    def get_all(self) -> dict[str, str]:
        return dict(self._load())

    def set(self, key: str, value: str) -> bool:
        try:
            self._client().write(self.item, {key: value})
        except (SecretsError, OSError) as error:
            print(f"OpSecrets: failed to set {key}: {error}", file=sys.stderr)
            return False
        self._cache = None
        return True


class VaultSecrets:
    """One Vault KV v2 path, read and merge-written through the SDK's HTTP adapter."""

    class VaultError(Exception):
        pass

    class VaultAuthError(VaultError):
        pass

    class VaultConnectionError(VaultError):
        pass

    class VaultSecretNotFoundError(VaultError):
        pass

    def __init__(
        self, path: str, token: str | None = None, addr: str | None = None, backend=None
    ):
        self.path = path
        self.token = token or vault_token()
        self.addr = addr or vault_address()
        self._backend = backend
        self._cache: dict[str, str] | None = None

    def _client(self) -> VaultKvBackend:
        if self._backend is None:
            if not self.token:
                raise self.VaultAuthError(
                    "\n❌ VAULT_TOKEN not set\n"
                    "The iac-runner passes its AppRole token to deploy tasks. For a "
                    "break-glass session mint a short-lived token with the root token "
                    "(`vault token create -ttl=1h`, bootstrap/05.vault README) and export "
                    "it as VAULT_TOKEN."
                )
            self._backend = _require_sdk(VaultKvBackend)(self.addr, token=self.token)
        return self._backend

    def _translate(self, error: SecretsError, verb: str) -> VaultError:
        text = str(error)
        if "HTTP 403" in text or "HTTP 401" in text:
            return self.VaultAuthError(
                f"\n❌ Permission denied ({verb}): {self.path}\nCheck: vault token lookup"
            )
        if "HTTP 503" in text or "HTTP 502" in text:
            return self.VaultConnectionError("\n❌ Vault is sealed or unavailable")
        return self.VaultError(f"\n❌ Vault {verb} failed: {text}\nPath: {self.path}")

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            try:
                data = dict(self._client().read(self.path))
            except SecretsError as error:
                raise self._translate(error, "read") from error
            if not data:
                raise self.VaultSecretNotFoundError(
                    f"\n❌ Secret not found: {self.path}"
                )
            self._cache = data
        return self._cache

    def get(self, key: str) -> Optional[str]:
        return self._load().get(key)

    def get_all(self) -> dict[str, str]:
        return dict(self._load())

    def set(self, key: str, value: str) -> bool:
        try:
            self._client().write(self.path, {key: value})
        except SecretsError as error:
            raise self._translate(error, "write") from error
        self._cache = None
        return True


def get_secrets(
    project: str,
    service: str | None = None,
    env: str = "production",
    credential_type: CredentialType | None = None,
) -> OpSecrets | VaultSecrets:
    """Factory: bootstrap / root_vars → 1Password items; app_vars (default) → Vault path."""
    validated_project: str = _validate_scope_value("project", project)  # type: ignore[assignment]
    validated_env: str = _validate_scope_value("env", env)  # type: ignore[assignment]
    validated_service = _validate_scope_value("service", service, allow_none=True)
    resolved_type = credential_type or "app_vars"
    if resolved_type == "bootstrap":
        return OpSecrets(
            item=f"{validated_project}/{validated_service}"
            if validated_service
            else validated_project
        )
    if resolved_type == "root_vars":
        item = (
            f"{validated_project}/{validated_env}/{validated_service}"
            if validated_service
            else f"{validated_project}/{validated_env}"
        )
        return OpSecrets(item=item)
    path = (
        f"{validated_project}/{validated_env}/{validated_service}"
        if validated_service
        else f"{validated_project}/{validated_env}"
    )
    return VaultSecrets(path=path)


def verify_vault_token(
    token: str,
    addr: str | None = None,
    min_ttl_hours: int = 24,
) -> dict:
    """Verify a Vault token is valid and has sufficient TTL.

    Args:
        token: The Vault token to verify
        addr: Vault address (optional, uses VAULT_ADDR or INTERNAL_DOMAIN)
        min_ttl_hours: Minimum acceptable TTL in hours (default: 24)

    Returns:
        dict with keys:
            - valid: bool
            - ttl_hours: float (remaining TTL, or -1 if expired/invalid)
            - renewable: bool
            - error: str | None
    """
    if not addr:
        addr = os.getenv("VAULT_ADDR")
        if not addr:
            domain = os.getenv("INTERNAL_DOMAIN")
            addr = f"https://vault.{domain}" if domain else "https://vault.localhost"

    verify_ssl = os.getenv("VAULT_SKIP_VERIFY", "").lower() not in ("1", "true", "yes")

    try:
        with httpx.Client(verify=verify_ssl, timeout=10.0) as client:
            resp = client.get(
                f"{addr}/v1/auth/token/lookup-self",
                headers={"X-Vault-Token": token},
            )

            if resp.status_code == 200:
                data = resp.json().get("data", {})
                ttl_seconds = data.get("ttl", 0)
                ttl_hours = ttl_seconds / 3600
                renewable = data.get("renewable", False)

                is_valid = ttl_hours >= min_ttl_hours

                return {
                    "valid": is_valid,
                    "ttl_hours": round(ttl_hours, 2),
                    "renewable": renewable,
                    "error": None
                    if is_valid
                    else f"TTL too low: {ttl_hours:.1f}h < {min_ttl_hours}h",
                }
            elif resp.status_code == 403:
                return {
                    "valid": False,
                    "ttl_hours": -1,
                    "renewable": False,
                    "error": "Token expired or invalid (403 Forbidden)",
                }
            else:
                return {
                    "valid": False,
                    "ttl_hours": -1,
                    "renewable": False,
                    "error": f"Vault returned status {resp.status_code}",
                }

    except httpx.ConnectError as e:
        return {
            "valid": False,
            "ttl_hours": -1,
            "renewable": False,
            "error": f"Cannot connect to Vault: {e}",
        }
    except httpx.TimeoutException:
        return {
            "valid": False,
            "ttl_hours": -1,
            "renewable": False,
            "error": "Vault connection timeout",
        }
