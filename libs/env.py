"""Secret-store access for infra2 tasks, over the infra2-sdk adapters (plan PR-E).

Three credential types, two stores:

- bootstrap: 1Password ``{project}/{service}`` — provisioning credentials, no env layer
- root_vars: 1Password ``{project}/{env}/{service}`` — human-entered values per env
- app_vars:  Vault ``secret/{project}/{env}/{service}`` — what services read at runtime

The Vault token comes from ``VAULT_TOKEN`` (the runner's bounded AppRole token, or a
break-glass token minted by ``bootstrap/05.vault``). ``VAULT_ROOT_TOKEN`` is also
accepted: it is the name the operator READMEs export for a hand-run task, and the
iac-runner forwards it next to ``VAULT_TOKEN`` into an invoke child whenever it
resolves a token at all, so a deploy of an older iac_ref still finds one.

Either way the value must already be in the process environment: no code path here
resolves a token from 1Password. The human exporting it is the one running ``op read``.
"""

from __future__ import annotations

import os
import secrets as _secrets
import string
import sys
from typing import Literal, Optional

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
            except (SecretsError, OSError, ModuleNotFoundError) as error:
                # ModuleNotFoundError: no infra2-sdk in a minimal GitHub Actions job
                # (deploy_v2, watchdogs) — same degrade as a missing `op` binary, which
                # is what those jobs always had (#649 incident: v1.1.66 crashed them).
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
        except (SecretsError, OSError, ModuleNotFoundError) as error:
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
            if VaultKvBackend is None:
                # A token but no SDK (minimal CI job): the historical "cannot reach Vault
                # from here" class, which callers such as libs.deploy.promote degrade on.
                raise self.VaultConnectionError(f"\n❌ {_SDK_MISSING}")
            self._backend = VaultKvBackend(self.addr, token=self.token)
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
    """Verify a Vault token is valid and has sufficient TTL (infra2-sdk ``vault_token_status``).

    Returns the historical dict shape callers and tests rely on:
    ``valid`` / ``ttl_hours`` / ``renewable`` / ``error`` (``None`` when valid).
    """
    address = addr or vault_address()
    try:
        from infra2_sdk.secrets import vault_token_status
    except ModuleNotFoundError:
        return {
            "valid": False,
            "ttl_hours": -1,
            "renewable": False,
            "error": _SDK_MISSING,
        }
    status = vault_token_status(address, token, min_ttl_seconds=min_ttl_hours * 3600)
    return {
        "valid": status.valid,
        "ttl_hours": status.ttl_hours if status.ttl_seconds >= 0 else -1,
        "renewable": status.renewable,
        "error": None if status.valid else status.error,
    }
