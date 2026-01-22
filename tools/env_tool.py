"""Environment variable and secrets CLI tool

Usage:
    invoke env.get KEY --project=platform --service=postgres [--type=app_vars]
    invoke env.set KEY=VALUE --project=platform --service=postgres [--type=root_vars]
    invoke env.list-all --project=platform --service=postgres [--type=app_vars]

Types:
    bootstrap  - 1Password: bootstrap project credentials
    root_vars  - 1Password: superadmin passwords for non-bootstrap services
    app_vars   - Vault: application variables (default)
"""

from __future__ import annotations

from typing import cast

from invoke import task

from libs.console import console, error, header, success
from libs.env import CredentialType, OpSecrets, get_secrets


VALID_TYPES: tuple[CredentialType, ...] = ("bootstrap", "root_vars", "app_vars")


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
):
    """Get secret from SSOT (Vault or 1Password)"""
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
):
    """Set secret in SSOT (Vault or 1Password)"""
    if "=" not in keyvalue:
        error("Format: KEY=VALUE")
        return
    validated_type = _validate_type(credential_type)
    if credential_type is not None and validated_type is None:
        return
    key, value = keyvalue.split("=", 1)
    secrets = get_secrets(project, service, env, credential_type=validated_type)
    if secrets.set(key, value):
        success(f"Set {key}")
    else:
        error(f"Failed to set {key}")


@task
def list_all(
    c,
    project: str = "platform",
    service: str | None = None,
    env: str = "production",
    credential_type: str | None = None,
):
    """List all secrets for a service"""
    from rich.table import Table

    if not service:
        error("--service is required")
        return

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
