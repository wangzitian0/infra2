"""Environment variable and secrets tool (remote-first, no local storage)

SSOT per project:
- bootstrap: 1Password for both env vars and secrets
- platform/others: Dokploy for env vars, Vault for secrets

Three-tier structure (matches Dokploy):
- Project: {project}
- Environment: {project}/{env}
- Service: {project}/{env}/{service}
"""
from __future__ import annotations
from invoke import task
import json
import os
import subprocess
from pathlib import Path
from typing import Optional, Any

# SSOT configuration per project
SSOT_CONFIG = {
    'bootstrap': {
        'env_source': '1password',
        'secret_source': '1password',
        'op_vault': 'infra2-bootstrap',
    },
    'platform': {
        'env_source': 'dokploy',
        'secret_source': 'vault',
    },
}

VALID_PROJECTS = list(SSOT_CONFIG.keys())
VALID_ENVS = ['production', 'staging']


def _get_ssot_path(project: str, env: str = None, service: str = None) -> str:
    """Build SSOT path for Vault/1Password"""
    parts = [project]
    if env:
        parts.append(env)
    if service:
        parts.append(service)
    return "/".join(parts)


# =============================================================================
# 1Password Operations
# =============================================================================

def _op_cmd(cmd: str) -> tuple[bool, str]:
    """Run 1Password CLI command"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def _op_get_item(op_vault: str, item_path: str) -> dict:
    """Get item from 1Password, returns dict of fields"""
    ok, output = _op_cmd(f'op item get "{item_path}" --vault="{op_vault}" --format=json')
    if not ok:
        return {}
    try:
        item = json.loads(output)
        return {f["label"]: f.get("value", "") for f in item.get("fields", []) 
                if f.get("label") and f.get("label") not in ["notesPlain", "password"]}
    except json.JSONDecodeError:
        return {}


def _op_get_field(op_vault: str, item_path: str, key: str) -> Optional[str]:
    """Get single field from 1Password item"""
    data = _op_get_item(op_vault, item_path)
    return data.get(key)


def _op_set_field(op_vault: str, item_path: str, key: str, value: str) -> bool:
    """Set single field in 1Password item (create if not exists)"""
    ok, _ = _op_cmd(f'op item get "{item_path}" --vault="{op_vault}"')
    if ok:
        # Update existing
        ok, output = _op_cmd(f'op item edit "{item_path}" --vault="{op_vault}" "{key}={value}"')
    else:
        # Create new
        ok, output = _op_cmd(f'op item create --category=login --title="{item_path}" --vault="{op_vault}" "{key}={value}"')
    return ok


# =============================================================================
# Vault Operations
# =============================================================================

def _vault_cmd(cmd: str) -> tuple[bool, str]:
    """Run vault command"""
    domain = os.environ.get("INTERNAL_DOMAIN", "")
    vault_addr = f"https://vault.{domain}" if domain else os.environ.get("VAULT_ADDR", "")
    env = os.environ.copy()
    if vault_addr:
        env["VAULT_ADDR"] = vault_addr
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True, env=env)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def _vault_get_all(path: str) -> dict:
    """Get all secrets from Vault path"""
    ok, output = _vault_cmd(f"vault kv get -format=json secret/{path}")
    if not ok:
        return {}
    try:
        return json.loads(output).get("data", {}).get("data", {})
    except json.JSONDecodeError:
        return {}


def _vault_get_field(path: str, key: str) -> Optional[str]:
    """Get single field from Vault"""
    data = _vault_get_all(path)
    return data.get(key)


def _vault_set_field(path: str, key: str, value: str) -> bool:
    """Set single field in Vault (merge with existing)"""
    existing = _vault_get_all(path)
    existing[key] = value
    kv_pairs = " ".join(f'{k}="{v}"' for k, v in existing.items())
    ok, _ = _vault_cmd(f"vault kv put secret/{path} {kv_pairs}")
    return ok


# =============================================================================
# Dokploy Operations (placeholder - needs API integration)
# =============================================================================

def _dokploy_get_all(project: str, env: str = None, service: str = None) -> dict:
    """Get env vars from Dokploy (placeholder)"""
    # TODO: Implement Dokploy API integration
    print(f"⚠️ Dokploy API not yet implemented. Path: {project}/{env}/{service}")
    return {}


def _dokploy_get_field(project: str, env: str, service: str, key: str) -> Optional[str]:
    """Get single env var from Dokploy"""
    data = _dokploy_get_all(project, env, service)
    return data.get(key)


def _dokploy_set_field(project: str, env: str, service: str, key: str, value: str) -> bool:
    """Set single env var in Dokploy (placeholder)"""
    # TODO: Implement Dokploy API integration
    print(f"⚠️ Dokploy API not yet implemented. Would set {key}={value} at {project}/{env}/{service}")
    return False


# =============================================================================
# Unified Interface
# =============================================================================

def _get_env(project: str, env: str, service: str, key: str) -> Optional[str]:
    """Get environment variable from appropriate SSOT"""
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    source = config['env_source']
    path = _get_ssot_path(project, env, service)
    
    if source == '1password':
        return _op_get_field(config['op_vault'], path, key)
    elif source == 'dokploy':
        return _dokploy_get_field(project, env, service, key)
    return None


def _set_env(project: str, env: str, service: str, key: str, value: str) -> bool:
    """Set environment variable in appropriate SSOT"""
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    source = config['env_source']
    path = _get_ssot_path(project, env, service)
    
    if source == '1password':
        return _op_set_field(config['op_vault'], path, key, value)
    elif source == 'dokploy':
        return _dokploy_set_field(project, env, service, key, value)
    return False


def _get_secret(project: str, env: str, service: str, key: str) -> Optional[str]:
    """Get secret from appropriate SSOT"""
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    source = config['secret_source']
    path = _get_ssot_path(project, env, service)
    
    if source == '1password':
        return _op_get_field(config['op_vault'], path, key)
    elif source == 'vault':
        return _vault_get_field(path, key)
    return None


def _set_secret(project: str, env: str, service: str, key: str, value: str) -> bool:
    """Set secret in appropriate SSOT"""
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    source = config['secret_source']
    path = _get_ssot_path(project, env, service)
    
    if source == '1password':
        return _op_set_field(config['op_vault'], path, key, value)
    elif source == 'vault':
        return _vault_set_field(path, key, value)
    return False


def _get_all_env(project: str, env: str, service: str) -> dict:
    """Get all environment variables"""
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    source = config['env_source']
    path = _get_ssot_path(project, env, service)
    
    if source == '1password':
        return _op_get_item(config['op_vault'], path)
    elif source == 'dokploy':
        return _dokploy_get_all(project, env, service)
    return {}


def _get_all_secrets(project: str, env: str, service: str) -> dict:
    """Get all secrets"""
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    source = config['secret_source']
    path = _get_ssot_path(project, env, service)
    
    if source == '1password':
        return _op_get_item(config['op_vault'], path)
    elif source == 'vault':
        return _vault_get_all(path)
    return {}


# =============================================================================
# Invoke Tasks
# =============================================================================

@task
def get(c, key: str, project: str, env: str = 'production', service: str = None):
    """Get environment variable from remote SSOT
    
    Example: invoke env.get API_KEY --project=platform --env=production --service=postgres
    """
    value = _get_env(project, env, service, key)
    if value:
        print(value)
    else:
        print(f"❌ Key '{key}' not found")


@task
def set(c, keyvalue: str, project: str, env: str = 'production', service: str = None):
    """Set environment variable in remote SSOT
    
    Example: invoke env.set API_KEY=value --project=platform --env=production
    """
    if '=' not in keyvalue:
        print("❌ Format: KEY=VALUE")
        return
    key, value = keyvalue.split('=', 1)
    if _set_env(project, env, service, key, value):
        print(f"✅ Set {key}")
    else:
        print(f"❌ Failed to set {key}")


@task(name="secret-get")
def secret_get(c, key: str, project: str, env: str = 'production', service: str = None):
    """Get secret from remote SSOT (Vault or 1Password)
    
    Example: invoke env.secret-get DB_PASSWORD --project=platform --env=production
    """
    value = _get_secret(project, env, service, key)
    if value:
        print(value)
    else:
        print(f"❌ Secret '{key}' not found")


@task(name="secret-set")
def secret_set(c, keyvalue: str, project: str, env: str = 'production', service: str = None):
    """Set secret in remote SSOT (Vault or 1Password)
    
    Example: invoke env.secret-set DB_PASSWORD=xxx --project=platform --env=production
    """
    if '=' not in keyvalue:
        print("❌ Format: KEY=VALUE")
        return
    key, value = keyvalue.split('=', 1)
    if _set_secret(project, env, service, key, value):
        print(f"✅ Set secret {key}")
    else:
        print(f"❌ Failed to set secret {key}")


@task
def preview(c, project: str, env: str = 'production', service: str = None):
    """Preview all environment variables and secrets for a service (no local storage)
    
    Shows variables with Dokploy-style references:
    - {VAR} - service level
    - {project.VAR} - project level
    - {environment.VAR} - environment level
    
    Example: invoke env.preview --project=platform --env=production --service=postgres
    """
    config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
    
    if not service:
        print("❌ --service is required for preview")
        return
    
    path = _get_ssot_path(project, env, service)
    
    print(f"\n📋 Preview: {path}")
    print(f"   Env Source: {config['env_source']}")
    print(f"   Secret Source: {config['secret_source']}")
    
    # Get vars from all levels
    project_vars = _get_all_env(project, None, None) or {}
    env_vars_level = _get_all_env(project, env, None) or {}
    service_vars = _get_all_env(project, env, service) or {}
    
    print("\n🔧 Environment Variables:")
    
    # Project level
    for k, v in project_vars.items():
        display = f"{v[:20]}..." if len(str(v)) > 20 else v
        print(f"   {{project.{k}}}={display}")
    
    # Environment level
    for k, v in env_vars_level.items():
        display = f"{v[:20]}..." if len(str(v)) > 20 else v
        print(f"   {{environment.{k}}}={display}")
    
    # Service level
    for k, v in service_vars.items():
        display = f"{v[:20]}..." if len(str(v)) > 20 else v
        print(f"   {{{k}}}={display}")
    
    # Get secrets
    project_secrets = _get_all_secrets(project, None, None) or {}
    env_secrets = _get_all_secrets(project, env, None) or {}
    service_secrets = _get_all_secrets(project, env, service) or {}
    
    if project_secrets or env_secrets or service_secrets:
        print("\n🔐 Secrets:")
        for k in project_secrets.keys():
            print(f"   {{project.{k}}}=********")
        for k in env_secrets.keys():
            print(f"   {{environment.{k}}}=********")
        for k in service_secrets.keys():
            print(f"   {{{k}}}=********")


@task
def copy(c, from_project: str, from_env: str, to_env: str, to_project: str = None, service: str = None):
    """Copy environment variables from one environment to another
    
    Example: invoke env.copy --from-project=platform --from-env=staging --to-env=production
    """
    to_project = to_project or from_project
    
    print(f"📋 Copying {from_project}/{from_env} → {to_project}/{to_env}")
    
    # Copy env vars
    env_vars = _get_all_env(from_project, from_env, service)
    for k, v in env_vars.items():
        _set_env(to_project, to_env, service, k, v)
        print(f"   ✅ {k}")
    
    # Copy secrets
    secrets = _get_all_secrets(from_project, from_env, service)
    for k, v in secrets.items():
        _set_secret(to_project, to_env, service, k, v)
        print(f"   🔐 {k}")
    
    print(f"\n✅ Copied {len(env_vars)} env vars and {len(secrets)} secrets")
