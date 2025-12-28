"""Environment variable sync tools

Three-tier structure (matches Dokploy):
- Project: {project}/.env -> Vault: secret/{project}/
- Environment: {project}/.env.{env} -> Vault: secret/{project}/{env}/
- Service: {project}/{service}/.env.{env} -> Vault: secret/{project}/{env}/{service}/
"""
from __future__ import annotations
from invoke import task
import json
import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv
from typing import Any

# Load env for INTERNAL_DOMAIN
load_dotenv()
load_dotenv('.env.local', override=True)

VALID_PROJECTS = ['bootstrap', 'platform', 'e2e_regression', 'tools']
VALID_ENVS = ['production', 'staging']


def _vault_cmd(c: Any, cmd: str, **kwargs) -> Any:
    """Run vault command with correct VAULT_ADDR"""
    domain = os.environ.get("INTERNAL_DOMAIN", "")
    vault_addr = f"https://vault.{domain}" if domain else ""
    if not vault_addr:
        print("⚠️ INTERNAL_DOMAIN not set, using default VAULT_ADDR")
        return c.run(cmd, **kwargs)
    return c.run(f"VAULT_ADDR={vault_addr} {cmd}", **kwargs)


def _validate_params(project: str, env: str) -> bool:
    """Validate project and env parameters"""
    if project not in VALID_PROJECTS:
        print(f"❌ Invalid project: {project}. Must be one of: {VALID_PROJECTS}")
        return False
    if not env.startswith('test_') and env not in VALID_ENVS:
        print(f"❌ Invalid env: {env}. Must be one of: {VALID_ENVS} or test_xxx")
        return False
    return True


def _resolve_path(root: Path, level: str, project: str, env: str, service: str | None = None) -> tuple[str | None, Path | None]:
    """Resolve vault path and local file path for a given level
    
    Three-tier structure:
    - Project: {project}/.env -> secret/{project}/
    - Environment: {project}/.env.{env} -> secret/{project}/{env}/
    - Service: {project}/{service}/.env.{env} -> secret/{project}/{env}/{service}/
    """
    project_dir = root / project
    
    if level == 'project':
        return project, project_dir / '.env'
    elif level == 'environment':
        return f"{project}/{env}", project_dir / f'.env.{env}'
    elif level == 'service':
        if not service:
            return None, None
        # Find service directory (may have numeric prefix like 01.postgres)
        svc_dir = None
        if project_dir.exists():
            for d in project_dir.iterdir():
                if d.is_dir() and (d.name == service or d.name.endswith(f'.{service}')):
                    svc_dir = d
                    break
        if not svc_dir:
            return None, None
        return f"{project}/{env}/{service}", svc_dir / f'.env.{env}'
    return None, None


@task
def pull(c, project: str, env: str = 'production', service: str | None = None, level: str = 'service'):
    """Pull from Vault to local .env
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        service: Service name (required for level=service)
        level: project, environment, or service
    """
    if not _validate_params(project, env):
        return
    
    root = Path(__file__).parent.parent
    vault_path, local_file = _resolve_path(root, level, project, env, service)
    
    if not vault_path:
        print(f"❌ Invalid level or missing service: {level}")
        return
    
    print(f"📥 Pulling {vault_path} → {local_file}")
    result = _vault_cmd(c, f"vault kv get -format=json secret/{vault_path}", hide=True, warn=True)
    if not result.ok:
        print(f"❌ Failed to read from Vault")
        return
    
    data = json.loads(result.stdout).get("data", {}).get("data", {})
    local_file.parent.mkdir(parents=True, exist_ok=True)
    with open(local_file, 'w') as f:
        for k, v in data.items():
            f.write(f"{k}={v}\n")
    print(f"✅ Wrote {len(data)} vars to {local_file}")


@task
def push(c, project: str, env: str = 'production', service: str | None = None, level: str = 'service'):
    """Push local .env to Vault
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        service: Service name (required for level=service)
        level: project, environment, or service
    """
    if not _validate_params(project, env):
        return
    
    root = Path(__file__).parent.parent
    vault_path, local_file = _resolve_path(root, level, project, env, service)
    
    if not vault_path:
        print(f"❌ Invalid level or missing service: {level}")
        return
    
    if not local_file.exists():
        print(f"❌ File not found: {local_file}")
        return
    
    data = dotenv_values(local_file)
    if not data:
        print(f"⚠️ No vars in {local_file}")
        return
    
    kv_pairs = " ".join(f"{k}={v}" for k, v in data.items())
    print(f"📤 Pushing {local_file} → {vault_path}")
    result = _vault_cmd(c, f"vault kv put secret/{vault_path} {kv_pairs}", warn=True, hide=True)
    if result.ok:
        print(f"✅ Pushed {len(data)} vars")
    else:
        print(f"❌ Failed to push")


@task
def status(c, project: str, env: str = 'production', service: str | None = None):
    """Show env var status
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        service: Service name (optional)
    """
    if not _validate_params(project, env):
        return
    
    root = Path(__file__).parent.parent
    print(f"Configuration for {project}/{env}:")
    
    for lvl in ['project', 'environment', 'service']:
        if lvl == 'service' and not service:
            continue
        vault_path, local_file = _resolve_path(root, lvl, project, env, service)
        exists = "✅" if local_file and local_file.exists() else "❌"
        print(f"  {lvl}: {exists} {local_file or 'N/A'}")
