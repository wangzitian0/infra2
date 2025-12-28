"""
Task loader for invoke automation
Discovers and loads tasks from bootstrap, platform, e2e_regression, and tools directories.
"""
from invoke import Collection, Task
from pathlib import Path
import importlib.util
import sys
from dotenv import load_dotenv

# Load .env first, then .env.local for secrets (overrides)
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
        
        # Load shared_tasks.py
        f = comp_dir / "shared_tasks.py"
        if f.exists():
            m = _load_module(f, f"{project_name}.{comp_dir.name}.shared")
            if m:
                shared = Collection()
                _add_tasks(m, shared)
                if shared.tasks:
                    coll.add_collection(shared, name="shared")
                    loaded = True
        
        # Load deploy.py
        f = comp_dir / "deploy.py"
        if f.exists():
            m = _load_module(f, f"{project_name}.{comp_dir.name}.deploy")
            if m:
                _add_tasks(m, coll)
                loaded = True
        
        # Load legacy tasks.py
        f = comp_dir / "tasks.py"
        if f.exists():
            m = _load_module(f, f"{project_name}.{comp_dir.name}.tasks")
            if m:
                _add_tasks(m, coll)
                loaded = True
        
        if loaded:
            ns.add_collection(coll, name=name)
            print(f"✅ {project_name}/{name}")


def _load_tools(ns, root):
    """Load tools as a special project"""
    tools_dir = root / "tools"
    if not tools_dir.exists():
        return
    
    # Load env_sync.py as env namespace
    f = tools_dir / "env_sync.py"
    if f.exists():
        m = _load_module(f, "tools.env_sync")
        if m:
            coll = Collection()
            _add_tasks(m, coll)
            if coll.tasks:
                ns.add_collection(coll, name="env")
                print("✅ tools/env")


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
