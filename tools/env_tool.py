"""Environment variable and secrets CLI tool

Wraps libs/env.py for command-line usage with Rich console output.

Usage:
    invoke env.get KEY --project=platform --env=production --service=postgres
    invoke env.set KEY=VALUE --project=platform --env=production
    invoke env.secret-get KEY --project=platform --env=production
    invoke env.secret-set KEY=VALUE --project=platform --env=production
    invoke env.preview --project=platform --env=production --service=postgres
"""
from __future__ import annotations
from invoke import task
from libs.env import EnvManager, SSOT_CONFIG
from libs.console import console, success, error, header


def _parse_keyvalue(keyvalue: str) -> tuple[str, str] | None:
    """Parse KEY=VALUE input."""
    if '=' not in keyvalue:
        error("Format: KEY=VALUE")
        return None
    key, value = keyvalue.split('=', 1)
    if not key:
        error("Key cannot be empty")
        return None
    return key, value


def _print_or_error(value: str | None, label: str) -> None:
    """Print value or error."""
    if value is not None:
        console.print(value)
    else:
        error(f"{label} not found")


@task
def get(c, key: str, project: str, env: str = 'production', service: str = None):
    """Get environment variable from remote SSOT"""
    mgr = EnvManager(project, env, service)
    _print_or_error(mgr.get_env(key), f"Key '{key}'")


@task(name="set")
def set_env(c, keyvalue: str, project: str, env: str = 'production', service: str = None):
    """Set environment variable in remote SSOT"""
    parsed = _parse_keyvalue(keyvalue)
    if not parsed:
        return
    key, value = parsed
    mgr = EnvManager(project, env, service)
    if mgr.set_env(key, value):
        success(f"Set {key}")
    else:
        error(f"Failed to set {key}")


@task(name="secret-get")
def secret_get(c, key: str, project: str, env: str = 'production', service: str = None):
    """Get secret from remote SSOT (Vault or 1Password)"""
    mgr = EnvManager(project, env, service)
    _print_or_error(mgr.get_secret(key), f"Secret '{key}'")


@task(name="secret-set")
def secret_set(c, keyvalue: str, project: str, env: str = 'production', service: str = None):
    """Set secret in remote SSOT (Vault or 1Password)"""
    parsed = _parse_keyvalue(keyvalue)
    if not parsed:
        return
    key, value = parsed
    mgr = EnvManager(project, env, service)
    if mgr.set_secret(key, value):
        success(f"Set secret {key}")
    else:
        error(f"Failed to set secret {key}")


@task
def preview(c, project: str, env: str = 'production', service: str = None):
    """Preview all environment variables and secrets for a service"""
    from rich.table import Table
    
    if not service:
        error("--service is required for preview")
        return
    
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    path = f"{project}/{env}/{service}"
    
    header(f"Preview: {path}", f"Env: {config['env_source']} | Secret: {config['secret_source']}")
    
    mgr_project = EnvManager(project, env, None)
    mgr_service = EnvManager(project, env, service)
    
    table = Table(show_header=True, title="Environment Variables")
    table.add_column("Level", style="cyan")
    table.add_column("Key")
    table.add_column("Value")
    
    for level, mgr in [("project", mgr_project), ("environment", mgr_project), ("service", mgr_service)]:
        for k, v in (mgr.get_all_env(level) or {}).items():
            table.add_row(level, k, _mask(v))
    
    console.print(table)
    
    # Secrets
    secrets_table = Table(show_header=True, title="Secrets")
    secrets_table.add_column("Level", style="cyan")
    secrets_table.add_column("Key")
    secrets_table.add_column("Value")
    
    for level, mgr in [("project", mgr_project), ("environment", mgr_project), ("service", mgr_service)]:
        for k in (mgr.get_all_secrets(level) or {}).keys():
            secrets_table.add_row(level, k, "********")
    
    console.print(secrets_table)


def _mask(value: str, max_len: int = 20) -> str:
    """Mask long values for display"""
    return f"{value[:max_len]}..." if len(str(value)) > max_len else value


@task
def copy(c, from_project: str, from_env: str, to_env: str, to_project: str = None, service: str = None):
    """Copy environment variables from one environment to another"""
    to_project = to_project or from_project
    
    header(f"Copy: {from_project}/{from_env} → {to_project}/{to_env}")
    
    from_mgr = EnvManager(from_project, from_env, service)
    to_mgr = EnvManager(to_project, to_env, service)
    
    # Copy env vars
    env_vars = from_mgr.get_all_env()
    for k, v in env_vars.items():
        to_mgr.set_env(k, v)
        success(f"Copied {k}")
    
    # Copy secrets
    secrets = from_mgr.get_all_secrets()
    for k, v in secrets.items():
        to_mgr.set_secret(k, v)
        success(f"Copied secret {k}")
    
    console.print(f"\n[green]✅ Copied {len(env_vars)} env vars and {len(secrets)} secrets[/]")
