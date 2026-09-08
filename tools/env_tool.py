"""Environment variable and secrets CLI tool

Usage:
    invoke env.get KEY --project=platform --service=postgres [--type=app_vars]
    invoke env.set KEY=VALUE --project=platform --service=postgres [--type=root_vars]
    invoke env.list-all --project=platform --service=postgres [--type=app_vars]

Types:
    bootstrap  - 1Password: bootstrap project credentials
    root_vars  - 1Password: human-entered values per environment
    app_vars   - Vault: what services read at runtime (default)

Nobody types into Vault (docs/ssot/bootstrap.vars_and_secrets.md §1.4): the deploy-time
supply (libs/secrets_supply.py) fills it from the manifest — human values are copied
from 1Password, runtime values are generated. ``env.set --type=app_vars`` therefore
refuses unless ``--break-glass`` is given, and the daily reconcile reports the drift.
"""

from __future__ import annotations

import sys
from typing import cast

from invoke import task

from libs.console import console, error, header, success
from libs.env import CredentialType, OpSecrets, get_secrets


VALID_TYPES: tuple[CredentialType, ...] = ("bootstrap", "root_vars", "app_vars")


def vault_write_guidance(project: str, service: str | None, env: str, key: str) -> str:
    """Why a hand write to Vault is refused, and what to do instead."""
    item = f"{project}/{env}/{service or '<service>'}"
    shared = f"{project}/shared/{service or '<service>'}"
    return (
        f"Refusing to write {key} into Vault {project}/{env}/{service or '<service>'} by hand.\n"
        f"  human value  → edit the 1Password item '{item}' (or '{shared}' for project-scoped "
        "keys) and redeploy; the supply copies it into Vault.\n"
        "  runtime value → redeploy; the supply generates what Vault lacks.\n"
        "  break-glass   → add --break-glass; the write is logged and the next reconcile "
        "reports it as drift."
    )


def write_secret(
    project: str,
    service: str | None,
    env: str,
    credential_type: CredentialType | None,
    key: str,
    value: str,
    *,
    break_glass: bool = False,
    secrets_factory=None,
    warn=lambda text: print(text, file=sys.stderr),
) -> tuple[bool, str]:
    """The env.set decision: 1Password writes are routine, Vault writes are break-glass."""
    if secrets_factory is None:
        secrets_factory = (
            get_secrets  # resolved at call time so tests can swap the backend
        )
    resolved = credential_type or "app_vars"
    if resolved == "app_vars":
        if not break_glass:
            return False, vault_write_guidance(project, service, env, key)
        warn(
            f"BREAK-GLASS: writing {key} to Vault {project}/{env}/{service} by hand; "
            "the next deploy's supply may overwrite it and the daily reconcile will report it."
        )
    secrets = secrets_factory(project, service, env, credential_type=credential_type)
    if secrets.set(key, value):
        return True, f"Set {key}"
    return False, f"Failed to set {key}"


def _validate_type(type_value: str | None) -> CredentialType | None:
    """Validate --type parameter"""
    if type_value is None:
        return None
    if type_value not in VALID_TYPES:
        error(f"Invalid --type: {type_value}. Must be one of: {', '.join(VALID_TYPES)}")
        return None
    return cast(CredentialType, type_value)


@task
def get(
    c,
    key: str,
    project: str = "platform",
    service: str | None = None,
    env: str = "production",
    credential_type: str | None = None,
    type: str | None = None,  # noqa: A002 - the documented --type spelling
):
    """Get secret from SSOT (Vault or 1Password)"""
    credential_type = credential_type or type
    validated_type = _validate_type(credential_type)
    if credential_type is not None and validated_type is None:
        return
    secrets = get_secrets(project, service, env, credential_type=validated_type)
    value = secrets.get(key)
    if value:
        console.print(value)
    else:
        error(f"Key '{key}' not found")


@task(name="set")
def set_secret(
    c,
    keyvalue: str,
    project: str = "platform",
    service: str | None = None,
    env: str = "production",
    credential_type: str | None = None,
    break_glass: bool = False,
    type: str | None = None,  # noqa: A002 - the documented --type spelling
):
    """Set a secret in 1Password; Vault only with --break-glass (the supply owns it)"""
    if "=" not in keyvalue:
        error("Format: KEY=VALUE")
        return
    credential_type = credential_type or type
    validated_type = _validate_type(credential_type)
    if credential_type is not None and validated_type is None:
        return
    key, value = keyvalue.split("=", 1)
    ok, message = write_secret(
        project, service, env, validated_type, key, value, break_glass=break_glass
    )
    if ok:
        success(message)
    else:
        error(message)


@task
def list_all(
    c,
    project: str = "platform",
    service: str | None = None,
    env: str = "production",
    credential_type: str | None = None,
    type: str | None = None,  # noqa: A002 - the documented --type spelling
):
    """List all secrets for a service"""
    from rich.table import Table

    if not service:
        error("--service is required")
        return

    credential_type = credential_type or type
    validated_type = _validate_type(credential_type)
    if credential_type is not None and validated_type is None:
        return

    secrets = get_secrets(project, service, env, credential_type=validated_type)
    data = secrets.get_all()

    type_label = credential_type or "app_vars"
    header(f"Secrets [{type_label}]: {project}/{env}/{service}")

    table = Table(show_header=True)
    table.add_column("Key")
    table.add_column("Value (masked)")

    for k, v in data.items():
        masked = f"{v[:4]}..." if len(str(v)) > 4 else "****"
        table.add_row(k, masked)

    console.print(table)


@task
def init_status(c):
    """Show init config from 1Password"""
    from rich.table import Table

    header("Init Config (1Password)")
    op = OpSecrets()
    data = op.get_all()

    table = Table(show_header=True)
    table.add_column("Key")
    table.add_column("Value")

    for k, v in data.items():
        table.add_row(k, v[:20] + "..." if len(str(v)) > 20 else v)

    console.print(table)
