"""
Infrastructure automation tasks - Root task file

This file automatically loads tasks.py from each module.
"""
from invoke import Collection
from pathlib import Path
import importlib.util
import sys
import os
from dotenv import load_dotenv
from invoke import task

# Load .env file
load_dotenv()

@task
def check_env(c):
    """Global environment variable check"""
    missing = []
    for var in ["VPS_HOST", "INTERNAL_DOMAIN"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"\n❌ ERROR: Missing global environment variables: {', '.join(missing)}")
        exit(1)
    print("✅ Global environment variable check passed")


# 创建根 namespace
ns = Collection()
ns.add_task(check_env)

# Automatically load tasks from modules
for module_dir_name in ["bootstrap", "platform"]:
    module_parent_dir = Path(__file__).parent / module_dir_name
    if not module_parent_dir.exists():
        continue
        
    for component_dir in sorted(module_parent_dir.iterdir()):
        if component_dir.is_dir():
            tasks_file = component_dir / "tasks.py"
            if tasks_file.exists():
                # Dynamically import module
                module_name = f"{module_dir_name}.{component_dir.name}.tasks"
                spec = importlib.util.spec_from_file_location(module_name, tasks_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)
                    
                    # Add to namespace, use component name as prefix
                    component_name = component_dir.name.split('.')[-1]  # Remove numeric prefix
                    ns.add_collection(Collection.from_module(module), name=component_name)
                    print(f"✅ Loaded {module_dir_name} module: {component_name}")


# Invoke will automatically look for this variable
# Usage: 
# uv run invoke vault.setup
# uv run invoke vault.prepare
