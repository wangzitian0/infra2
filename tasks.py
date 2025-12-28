"""
Infrastructure automation tasks
"""
from invoke import Collection, task, Task
from pathlib import Path
import importlib.util
import sys
import os
from dotenv import load_dotenv

load_dotenv()


@task
def check_env(c):
    """Check required environment variables"""
    from libs.common import validate_env
    missing = validate_env()
    if missing:
        print(f"❌ Missing: {', '.join(missing)}")
        exit(1)
    print("✅ Environment OK")


def _load_module(file_path, module_name):
    """Load a Python file as a module"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    return None


def _add_tasks(module, collection):
    """Add all tasks from a module to a collection"""
    for name in dir(module):
        obj = getattr(module, name)
        # Fix #2 & #3: Use proper Task check instead of magic attributes
        if isinstance(obj, Task):
            try:
                collection.add_task(obj)
            except ValueError as e:
                # Only catch "task already exists" errors
                if "already" not in str(e):
                    print(f"⚠️ Failed to add task {name}: {e}")


def _load_all():
    """Load all modules (wrapped in function for testability - Fix #9)"""
    ns = Collection()
    ns.add_task(check_env)
    
    root = Path(__file__).parent
    for layer in ["bootstrap", "platform"]:
        layer_dir = root / layer
        if not layer_dir.exists():
            continue
        
        for comp_dir in sorted(layer_dir.iterdir()):
            if not comp_dir.is_dir():
                continue
            
            name = comp_dir.name.split('.')[-1]
            coll = Collection()
            loaded = False
            
            # Load shared_tasks.py
            f = comp_dir / "shared_tasks.py"
            if f.exists():
                m = _load_module(f, f"{layer}.{comp_dir.name}.shared")
                if m:
                    shared = Collection()
                    _add_tasks(m, shared)
                    if shared.tasks:
                        coll.add_collection(shared, name="shared")
                        loaded = True
            
            # Load deploy.py
            f = comp_dir / "deploy.py"
            if f.exists():
                m = _load_module(f, f"{layer}.{comp_dir.name}.deploy")
                if m:
                    _add_tasks(m, coll)
                    loaded = True
            
            # Load legacy tasks.py
            f = comp_dir / "tasks.py"
            if f.exists():
                m = _load_module(f, f"{layer}.{comp_dir.name}.tasks")
                if m:
                    _add_tasks(m, coll)
                    loaded = True
            
            if loaded:
                ns.add_collection(coll, name=name)
                print(f"✅ {layer}/{name}")
    
    # Load env tools
    f = root / "tools" / "env_sync.py"
    if f.exists():
        m = _load_module(f, "tools.env_sync")
        if m:
            coll = Collection()
            _add_tasks(m, coll)
            if coll.tasks:
                ns.add_collection(coll, name="env")
                print("✅ tools/env")
    
    return ns


ns = _load_all()
