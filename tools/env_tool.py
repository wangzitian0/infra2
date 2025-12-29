"""Environment variable and secrets CLI tool

Simplified - uses new OpSecrets/VaultSecrets API.

Usage:
    invoke env.get KEY --project=platform --service=postgres
    invoke env.set KEY=VALUE --project=platform --service=postgres
    invoke env.list-all --project=platform --service=postgres
"""
from __future__ import annotations
from invoke import task
from libs.env import get_secrets, OpSecrets
from libs.console import console, success, error, header


@task
def get(c, key: str, project: str = "platform", service: str = None, env: str = 'production'):
    """Get secret from SSOT (Vault or 1Password)"""
    secrets = get_secrets(project, service, env)
    value = secrets.get(key)
    if value:
        console.print(value)
    else:
        error(f"Key '{key}' not found")


@task(name="set")
def set_secret(c, keyvalue: str, project: str = "platform", service: str = None, env: str = 'production'):
    """Set secret in SSOT (Vault or 1Password)"""
    if '=' not in keyvalue:
        error("Format: KEY=VALUE")
        return
    key, value = keyvalue.split('=', 1)
    secrets = get_secrets(project, service, env)
    if secrets.set(key, value):
        success(f"Set {key}")
    else:
        error(f"Failed to set {key}")


@task
def list_all(c, project: str = "platform", service: str = None, env: str = 'production'):
    """List all secrets for a service"""
    from rich.table import Table
    
    if not service:
        error("--service is required")
        return
    
    secrets = get_secrets(project, service, env)
    data = secrets.get_all()
    
    header(f"Secrets: {project}/{env}/{service}")
    
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
