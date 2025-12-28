"""Environment variable sync tools

Three-tier structure:
- Project: .env (root) -> Vault: secret/{project}/
- Environment: .env.<env> (root) -> Vault: secret/{project}/{env}/
- Service: {project}/.env.<env> -> Vault: secret/{project}/{env}/service/
"""
from invoke import task
import json
import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

# Load env for INTERNAL_DOMAIN
load_dotenv()
load_dotenv('.env.local', override=True)

VALID_PROJECTS = ['bootstrap', 'platform', 'e2e_regression', 'tools']
VALID_ENVS = ['production', 'staging']


def _vault_cmd(c, cmd: str, **kwargs):
    """Run vault command with correct VAULT_ADDR"""
    domain = os.environ.get("INTERNAL_DOMAIN", "")
    vault_addr = f"https://vault.{domain}" if domain else ""
    if not vault_addr:
        print("⚠️ INTERNAL_DOMAIN not set, using default VAULT_ADDR")
        return c.run(cmd, **kwargs)
    return c.run(f"VAULT_ADDR={vault_addr} {cmd}", **kwargs)


def _validate_params(project, env):
    """Validate project and env parameters"""
    if project not in VALID_PROJECTS:
        print(f"❌ Invalid project: {project}. Must be one of: {VALID_PROJECTS}")
        return False
    if not env.startswith('test_') and env not in VALID_ENVS:
        print(f"❌ Invalid env: {env}. Must be one of: {VALID_ENVS} or test_xxx")
        return False
    return True


def _resolve_path(root, level, project, env):
    """Resolve vault path and local file path for a given level"""
    if level == 'project':
        return project, root / '.env'
    elif level == 'environment':
        return f"{project}/{env}", root / f'.env.{env}'
    elif level == 'service':
        return f"{project}/{env}/service", root / project / f'.env.{env}'
    return None, None


@task
def pull(c, project, env='production', level='service'):
    """Pull from Vault to local .env
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        level: project, environment, or service
    """
    if not _validate_params(project, env):
        return
    
    root = Path(__file__).parent.parent
    vault_path, local_file = _resolve_path(root, level, project, env)
    
    if not vault_path:
        print(f"❌ Invalid level: {level}")
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
def push(c, project, env='production', level='service'):
    """Push local .env to Vault
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
        level: project, environment, or service
    """
    if not _validate_params(project, env):
        return
    
    root = Path(__file__).parent.parent
    vault_path, local_file = _resolve_path(root, level, project, env)
    
    if not vault_path:
        print(f"❌ Invalid level: {level}")
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
def status(c, project, env='production'):
    """Show env var status
    
    Args:
        project: bootstrap, platform, e2e_regression, or tools
        env: production, staging, or test_xxx
    """
    if not _validate_params(project, env):
        return
    
    root = Path(__file__).parent.parent
    print(f"Configuration for {project}/{env}:")
    
    for lvl in ['project', 'environment', 'service']:
        vault_path, local_file = _resolve_path(root, lvl, project, env)
        exists = "✅" if local_file and local_file.exists() else "❌"
        print(f"  {lvl}: {exists} {local_file or 'N/A'}")
