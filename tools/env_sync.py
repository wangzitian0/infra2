"""Environment variable sync tools"""
from invoke import task
import json
import os
from pathlib import Path
from dotenv import dotenv_values


def _resolve_path(root, level, project, env, service):
    """Resolve vault path and local file path for a given level"""
    if level == 'project':
        return project, root / '.env'
    elif level == 'environment':
        return f"{project}/{env}", root / f'.env.{env}'
    elif level == 'service':
        if not service:
            return None, None
        # Find service directory
        svc_dir = None
        for d in (root / project).iterdir():
            if d.is_dir() and (d.name == service or d.name.endswith(f'.{service}')):
                svc_dir = d
                break
        if not svc_dir:
            return None, None
        return f"{project}/{env}/{service}", svc_dir / f'.env.{env}.local'
    return None, None


@task
def pull(c, level, project='platform', env='prod', service=None):
    """Pull from Vault to local .env"""
    root = Path(__file__).parent.parent
    vault_path, local_file = _resolve_path(root, level, project, env, service)
    
    if not vault_path:
        print(f"❌ Invalid level or missing service")
        return
    
    print(f"📥 Pulling {vault_path} → {local_file}")
    result = c.run(f"vault kv get -format=json secret/{vault_path}", hide=True, warn=True)
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
def push(c, level, project='platform', env='prod', service=None):
    """Push local .env to Vault"""
    root = Path(__file__).parent.parent
    vault_path, local_file = _resolve_path(root, level, project, env, service)
    
    if not vault_path:
        print(f"❌ Invalid level or missing service")
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
    result = c.run(f"vault kv put secret/{vault_path} {kv_pairs}", warn=True, hide=True)
    if result.ok:
        print(f"✅ Pushed {len(data)} vars")
    else:
        print(f"❌ Failed to push")


@task
def status(c, project='platform', env='prod', service=None):
    """Show env var status"""
    root = Path(__file__).parent.parent
    print("Configuration:")
    
    for lvl in ['project', 'environment', 'service']:
        if lvl == 'service' and not service:
            continue
        vault_path, local_file = _resolve_path(root, lvl, project, env, service)
        exists = "✅" if local_file and local_file.exists() else "❌"
        print(f"  {lvl}: {exists} {local_file or 'N/A'}")
