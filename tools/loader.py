"""
Task loader for invoke automation
Discovers and loads tasks from bootstrap, platform, and tools directories.
"""
from __future__ import annotations

from invoke import Collection, Task
from pathlib import Path
import importlib.util
import sys
from typing import Optional
from dotenv import load_dotenv

# Load optional local overrides if present.
load_dotenv()
load_dotenv('.env.local', override=True)


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
        if isinstance(obj, Task):
            try:
                collection.add_task(obj)
            except ValueError as e:
                if "already" not in str(e):
                    print(f"⚠️ Failed to add task {name}: {e}")


def _load_tasks_into_collection(file_path: Path, module_name: str, collection: Collection, sub_name: Optional[str] = None) -> bool:
    """Load tasks from a file into a collection (or sub-collection)."""
    if not file_path.exists():
        return False
    module = _load_module(file_path, module_name)
    if not module:
        return False
    if sub_name:
        sub = Collection()
        _add_tasks(module, sub)
        if sub.tasks:
            collection.add_collection(sub, name=sub_name)
            return True
        return False
    before = len(collection.tasks)
    _add_tasks(module, collection)
    return len(collection.tasks) > before


def _load_project(ns, root, project_name):
    """Load all services from a project directory"""
    project_dir = root / project_name
    if not project_dir.exists():
        return
    
    for comp_dir in sorted(project_dir.iterdir()):
        if not comp_dir.is_dir():
            continue
        
        name = comp_dir.name.split('.')[-1]
        coll = Collection()
        loaded = False
        
        loaded |= _load_tasks_into_collection(
            comp_dir / "shared_tasks.py",
            f"{project_name}.{comp_dir.name}.shared",
            coll,
            sub_name="shared",
        )
        loaded |= _load_tasks_into_collection(
            comp_dir / "deploy.py",
            f"{project_name}.{comp_dir.name}.deploy",
            coll,
        )
        loaded |= _load_tasks_into_collection(
            comp_dir / "tasks.py",
            f"{project_name}.{comp_dir.name}.tasks",
            coll,
        )
        
        if loaded:
            ns.add_collection(coll, name=name)
            print(f"✅ {project_name}/{name}")


def _load_tools(ns, root):
    """Load tools as a special project"""
    tools_dir = root / "tools"
    if not tools_dir.exists():
        return
    
    coll = Collection()
    if _load_tasks_into_collection(tools_dir / "env_tool.py", "tools.env_tool", coll):
        ns.add_collection(coll, name="env")
        print("✅ tools/env")

    coll = Collection()
    if _load_tasks_into_collection(tools_dir / "local_init.py", "tools.local_init", coll):
        ns.add_collection(coll, name="local")
        print("✅ tools/local")


def load_all():
    """Load all modules from all projects"""
    from libs.common import validate_env
    from invoke import task
    
    @task
    def check_env(c):
        """Check required environment variables"""
        missing = validate_env()
        if missing:
            print(f"❌ Missing: {', '.join(missing)}")
            exit(1)
        print("✅ Environment OK")
    
    ns = Collection()
    ns.add_task(check_env)
    
    root = Path(__file__).parent.parent
    
    # Load projects
    for project in ["bootstrap", "platform"]:
        _load_project(ns, root, project)
    
    # Load tools
    _load_tools(ns, root)
    
    return ns


ns = load_all()
