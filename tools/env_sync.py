"""Environment variable sync tools

SSOT per project:
- bootstrap: 1Password (op CLI) for both env vars and secrets
- platform: Dokploy for env vars, Vault for secrets

Three-tier structure (matches Dokploy):
- Project: {project}/.env -> secret/{project}/
- Environment: {project}/.env.{env} -> secret/{project}/{env}/
- Service: {project}/{service}/.env.{env} -> secret/{project}/{env}/{service}/
"""
from __future__ import annotations
from invoke import task
import json
import os
import subprocess
from pathlib import Path
from dotenv import dotenv_values, load_dotenv
from typing import Any, Optional

# Load env for INTERNAL_DOMAIN
load_dotenv()
load_dotenv('.env.local', override=True)

VALID_PROJECTS = ['bootstrap', 'platform', 'e2e_regression', 'tools']
VALID_ENVS = ['production', 'staging']

# SSOT configuration per project
SSOT_CONFIG = {
    'bootstrap': {
        'env_source': '1password',  # 1Password is SSOT for bootstrap
        'secret_source': '1password',
    },
    'platform': {
        'env_source': 'dokploy',    # Dokploy is SSOT for platform env vars
        'secret_source': 'vault',    # Vault is SSOT for platform secrets
    },
    'e2e_regression': {
        'env_source': 'local',       # Local files only
        'secret_source': 'local',
    },
    'tools': {
        'env_source': 'local',
        'secret_source': 'local',
    },
}


def _vault_cmd(c: Any, cmd: str, **kwargs) -> Any:
    """Run vault command with correct VAULT_ADDR"""
    domain = os.environ.get("INTERNAL_DOMAIN", "")
    vault_addr = f"https://vault.{domain}" if domain else ""
    if not vault_addr:
        print("⚠️ INTERNAL_DOMAIN not set, using default VAULT_ADDR")
        return c.run(cmd, **kwargs)
    return c.run(f"VAULT_ADDR={vault_addr} {cmd}", **kwargs)


def _op_cmd(cmd: str) -> tuple[bool, str]:
    """Run 1Password CLI command"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, check=True
        )
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def _validate_params(project: str, env: str) -> bool:
    """Validate project and env parameters"""
    if project not in VALID_PROJECTS:
        print(f"❌ Invalid project: {project}. Must be one of: {VALID_PROJECTS}")
        return False
    if not env.startswith('test_') and env not in VALID_ENVS:
        print(f"❌ Invalid env: {env}. Must be one of: {VALID_ENVS} or test_xxx")
        return False
    return True


def _resolve_path(root: Path, level: str, project: str, env: str, service: Optional[str] = None) -> tuple[Optional[str], Optional[Path]]:
    """Resolve vault/1password path and local file path for a given level
    
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


def _get_1password_vault_name(project: str) -> str:
    """Get 1Password vault name for a project"""
    return f"infra2-{project}"


def _pull_from_vault(c: Any, vault_path: str, local_file: Path) -> bool:
    """Pull secrets from Vault to local file"""
    print(f"📥 [Vault] Pulling secret/{vault_path} → {local_file}")
    result = _vault_cmd(c, f"vault kv get -format=json secret/{vault_path}", hide=True, warn=True)
    if not result.ok:
        print(f"❌ Failed to read from Vault")
        return False
    
    data = json.loads(result.stdout).get("data", {}).get("data", {})
    local_file.parent.mkdir(parents=True, exist_ok=True)
    with open(local_file, 'w') as f:
        for k, v in data.items():
            f.write(f"{k}={v}\n")
    print(f"✅ Wrote {len(data)} vars to {local_file}")
    return True


def _push_to_vault(c: Any, vault_path: str, local_file: Path) -> bool:
    """Push local file to Vault"""
    if not local_file.exists():
        print(f"❌ File not found: {local_file}")
        return False
    
    data = dotenv_values(local_file)
    if not data:
        print(f"⚠️ No vars in {local_file}")
        return False
    
    kv_pairs = " ".join(f"{k}={v}" for k, v in data.items())
    print(f"📤 [Vault] Pushing {local_file} → secret/{vault_path}")
    result = _vault_cmd(c, f"vault kv put secret/{vault_path} {kv_pairs}", warn=True, hide=True)
    if result.ok:
        print(f"✅ Pushed {len(data)} vars to Vault")
        return True
    else:
        print(f"❌ Failed to push to Vault")
        return False


def _pull_from_1password(item_path: str, local_file: Path) -> bool:
    """Pull secrets from 1Password to local file
    
    Args:
        item_path: Path like "infra2-bootstrap/vault/production"
        local_file: Local file to write to
    """
    print(f"📥 [1Password] Pulling {item_path} → {local_file}")
    
    # Get item from 1Password
    ok, output = _op_cmd(f'op item get "{item_path}" --format=json')
    if not ok:
        print(f"❌ Failed to read from 1Password: {output}")
        return False
    
    try:
        item = json.loads(output)
        fields = item.get("fields", [])
        data = {}
        for field in fields:
            label = field.get("label", "")
            value = field.get("value", "")
            if label and value and label not in ["notesPlain", "password"]:
                data[label] = value
        
        local_file.parent.mkdir(parents=True, exist_ok=True)
        with open(local_file, 'w') as f:
            for k, v in data.items():
                f.write(f"{k}={v}\n")
        print(f"✅ Wrote {len(data)} vars to {local_file}")
        return True
    except json.JSONDecodeError:
        print(f"❌ Failed to parse 1Password response")
        return False


def _push_to_1password(item_path: str, local_file: Path) -> bool:
    """Push local file to 1Password
    
    Args:
        item_path: Path like "infra2-bootstrap/vault/production"
        local_file: Local file to read from
    """
    if not local_file.exists():
        print(f"❌ File not found: {local_file}")
        return False
    
    data = dotenv_values(local_file)
    if not data:
        print(f"⚠️ No vars in {local_file}")
        return False
    
    print(f"📤 [1Password] Pushing {local_file} → {item_path}")
    
    # Build field arguments
    field_args = " ".join(f'"{k}={v}"' for k, v in data.items())
    
    # Try to get existing item first
    ok, _ = _op_cmd(f'op item get "{item_path}"')
    if ok:
        # Update existing item
        ok, output = _op_cmd(f'op item edit "{item_path}" {field_args}')
    else:
        # Create new item
        vault_name = item_path.split("/")[0]
        item_name = "/".join(item_path.split("/")[1:])
        ok, output = _op_cmd(f'op item create --category=login --title="{item_name}" --vault="{vault_name}" {field_args}')
    
    if ok:
        print(f"✅ Pushed {len(data)} vars to 1Password")
        return True
    else:
        print(f"❌ Failed to push to 1Password: {output}")
        return False


@task
def pull(c, project: str, env: str = 'production', service: Optional[str] = None, level: str = 'service'):
    """Pull from SSOT to local .env
    
    SSOT per project:
    - bootstrap: 1Password
    - platform: Vault (secrets)
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        service: Service name (required for level=service)
        level: project, environment, or service
    """
    if not _validate_params(project, env):
        return
    
    config = SSOT_CONFIG.get(project, {})
    source = config.get('secret_source', 'local')
    
    root = Path(__file__).parent.parent
    ssot_path, local_file = _resolve_path(root, level, project, env, service)
    
    if not ssot_path:
        print(f"❌ Invalid level or missing service: {level}")
        return
    
    if source == 'vault':
        _pull_from_vault(c, ssot_path, local_file)
    elif source == '1password':
        op_vault = _get_1password_vault_name(project)
        op_item_path = f"{op_vault}/{ssot_path}"
        _pull_from_1password(op_item_path, local_file)
    elif source == 'local':
        print(f"ℹ️ Project {project} uses local files as SSOT, no pull needed")
    else:
        print(f"❌ Unknown source: {source}")


@task
def push(c, project: str, env: str = 'production', service: Optional[str] = None, level: str = 'service'):
    """Push local .env to SSOT
    
    SSOT per project:
    - bootstrap: 1Password
    - platform: Vault (secrets)
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        service: Service name (required for level=service)
        level: project, environment, or service
    """
    if not _validate_params(project, env):
        return
    
    config = SSOT_CONFIG.get(project, {})
    source = config.get('secret_source', 'local')
    
    root = Path(__file__).parent.parent
    ssot_path, local_file = _resolve_path(root, level, project, env, service)
    
    if not ssot_path:
        print(f"❌ Invalid level or missing service: {level}")
        return
    
    if source == 'vault':
        _push_to_vault(c, ssot_path, local_file)
    elif source == '1password':
        op_vault = _get_1password_vault_name(project)
        op_item_path = f"{op_vault}/{ssot_path}"
        _push_to_1password(op_item_path, local_file)
    elif source == 'local':
        print(f"ℹ️ Project {project} uses local files as SSOT, no push needed")
    else:
        print(f"❌ Unknown source: {source}")


@task
def status(c, project: str, env: str = 'production', service: Optional[str] = None):
    """Show env var status
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        service: Service name (optional)
    """
    if not _validate_params(project, env):
        return
    
    config = SSOT_CONFIG.get(project, {})
    print(f"\n📋 Configuration for {project}/{env}:")
    print(f"   SSOT: env={config.get('env_source')}, secrets={config.get('secret_source')}")
    print()
    
    root = Path(__file__).parent.parent
    for lvl in ['project', 'environment', 'service']:
        if lvl == 'service' and not service:
            continue
        ssot_path, local_file = _resolve_path(root, lvl, project, env, service)
        exists = "✅" if local_file and local_file.exists() else "❌"
        print(f"  {lvl:12} {exists} {local_file or 'N/A'}")
