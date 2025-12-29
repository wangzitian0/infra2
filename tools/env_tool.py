"""Environment variable and secrets CLI tool

Wraps libs/env.py for command-line usage.

Usage:
    invoke env.get KEY --project=platform --env=production --service=postgres
    invoke env.set KEY=VALUE --project=platform --env=production
    invoke env.secret-get KEY --project=platform --env=production
    invoke env.secret-set KEY=VALUE --project=platform --env=production
    invoke env.preview --project=platform --env=production --service=postgres
    invoke env.copy --from-project=platform --from-env=staging --to-env=production
"""
from __future__ import annotations
from invoke import task
from typing import Optional
from libs.env import EnvManager, SSOT_CONFIG


@task
def get(c, key: str, project: str, env: str = 'production', service: str = None):
    """Get environment variable from remote SSOT
    
    Example: invoke env.get API_KEY --project=platform --env=production --service=postgres
    """
    mgr = EnvManager(project, env, service)
    value = mgr.get_env(key)
    if value:
        print(value)
    else:
        print(f"❌ Key '{key}' not found")


@task(name="set")
def set_env(c, keyvalue: str, project: str, env: str = 'production', service: str = None):
    """Set environment variable in remote SSOT
    
    Example: invoke env.set API_KEY=value --project=platform --env=production
    """
    if '=' not in keyvalue:
        print("❌ Format: KEY=VALUE")
        return
    key, value = keyvalue.split('=', 1)
    mgr = EnvManager(project, env, service)
    if mgr.set_env(key, value):
        print(f"✅ Set {key}")
    else:
        print(f"❌ Failed to set {key}")


@task(name="secret-get")
def secret_get(c, key: str, project: str, env: str = 'production', service: str = None):
    """Get secret from remote SSOT (Vault or 1Password)
    
    Example: invoke env.secret-get DB_PASSWORD --project=platform --env=production
    """
    mgr = EnvManager(project, env, service)
    value = mgr.get_secret(key)
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
    mgr = EnvManager(project, env, service)
    if mgr.set_secret(key, value):
        print(f"✅ Set secret {key}")
    else:
        print(f"❌ Failed to set secret {key}")


@task
def preview(c, project: str, env: str = 'production', service: str = None):
    """Preview all environment variables and secrets for a service
    
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
    
    path = f"{project}/{env}/{service}"
    
    print(f"\n📋 Preview: {path}")
    print(f"   Env Source: {config['env_source']}")
    print(f"   Secret Source: {config['secret_source']}")
    
    # Get vars from all levels
    mgr_project = EnvManager(project, env, None)
    mgr_service = EnvManager(project, env, service)
    
    project_vars = mgr_project.get_all_env('project') or {}
    env_vars = mgr_project.get_all_env('environment') or {}
    service_vars = mgr_service.get_all_env('service') or {}
    
    print("\n🔧 Environment Variables:")
    for k, v in project_vars.items():
        display = f"{v[:20]}..." if len(str(v)) > 20 else v
        print(f"   {{project.{k}}}={display}")
    for k, v in env_vars.items():
        display = f"{v[:20]}..." if len(str(v)) > 20 else v
        print(f"   {{environment.{k}}}={display}")
    for k, v in service_vars.items():
        display = f"{v[:20]}..." if len(str(v)) > 20 else v
        print(f"   {{{k}}}={display}")
    
    # Get secrets
    project_secrets = mgr_project.get_all_secrets('project') or {}
    env_secrets = mgr_project.get_all_secrets('environment') or {}
    service_secrets = mgr_service.get_all_secrets('service') or {}
    
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
    
    from_mgr = EnvManager(from_project, from_env, service)
    to_mgr = EnvManager(to_project, to_env, service)
    
    # Copy env vars
    env_vars = from_mgr.get_all_env()
    for k, v in env_vars.items():
        to_mgr.set_env(k, v)
        print(f"   ✅ {k}")
    
    # Copy secrets
    secrets = from_mgr.get_all_secrets()
    for k, v in secrets.items():
        to_mgr.set_secret(k, v)
        print(f"   🔐 {k}")
    
    print(f"\n✅ Copied {len(env_vars)} env vars and {len(secrets)} secrets")
