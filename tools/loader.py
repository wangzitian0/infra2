"""
Task loader for invoke automation
Discovers and loads tasks from bootstrap, platform, and tools directories.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
from typing import Optional
from dotenv import load_dotenv
from invoke import Collection, Task
from common.console import success, error, warning
from common.core.environ import normalize_env_name

# Load optional local overrides if present.
load_dotenv()
load_dotenv(".env.local", override=True)


def _ensure_runner_sdk_compat() -> None:
    """Backfill v3.7+/v3.8+ SDK symbols when running on a pre-bootstrap iac-runner image.

    The shared L1 iac-runner container rebuilds only on Stage 3 production approval
    (.github/workflows/deploy.yml bootstrap job), while Stage 2 staging reconciles run
    immediately on release-tag push against the checked-out repository tree.
    """
    secrets_mod = importlib.import_module("infra2_sdk.secrets")
    if not hasattr(secrets_mod, "VaultTokenTarget"):

        @dataclass(frozen=True)
        class VaultTokenTarget:
            project: str
            service: str
            service_dir: str
            project_dir: str
            dokploy_project: str

        setattr(secrets_mod, "VaultTokenTarget", VaultTokenTarget)

    if not hasattr(secrets_mod, "normalize_selector"):

        def normalize_selector(value: str | None, *, label: str) -> str | None:
            if value is None:
                return None
            normalized = value.strip()
            if not normalized:
                raise ValueError(f"{label} must not be empty")
            if "-" in normalized or "/" in normalized:
                raise ValueError(f"{label} must not include '-' or '/'")
            return normalized

        setattr(secrets_mod, "normalize_selector", normalize_selector)

    if not hasattr(secrets_mod, "policy_name"):

        def policy_name(project: str, env: str, service: str) -> str:
            return f"{project}-{normalize_env_name(env)}-{service}"

        setattr(secrets_mod, "policy_name", policy_name)

    refs_mod = importlib.import_module("infra2_sdk.refs")
    if not hasattr(refs_mod, "assert_after_on_main"):

        def assert_after_on_main(
            after: str,
            repo_root: Path,
            *,
            base: str = "origin/main",
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        ) -> str:
            resolved = runner(
                ["git", "rev-parse", "--verify", f"{after}^{{commit}}"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if resolved.returncode != 0:
                raise SystemExit(
                    f"::error::cannot resolve {after!r} to a commit "
                    f"({resolved.stderr.strip() or 'unknown revision'})."
                )
            sha = resolved.stdout.strip()
            ancestor = runner(
                ["git", "merge-base", "--is-ancestor", sha, base],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if ancestor.returncode == 0:
                return sha
            if ancestor.returncode == 1:
                raise SystemExit(
                    f"::error::refusing to reconcile {after!r} ({sha[:12]}): not reachable from "
                    f"{base}. Release tags must be cut on reviewed main (Infra-011 invariant). "
                    f"Re-cut the tag on main, or pass --dry-run to plan only."
                )
            raise SystemExit(
                f"::error::cannot verify provenance of {after!r}: base ref {base!r} is "
                f"unresolvable ({ancestor.stderr.strip() or 'git error'})."
            )

        setattr(refs_mod, "assert_after_on_main", assert_after_on_main)

    if not hasattr(refs_mod, "resolve_branch_to_sha"):
        ls_remote_rows = getattr(refs_mod, "_ls_remote_rows", None) or getattr(
            refs_mod, "ls_remote_rows"
        )
        redact_repo = getattr(refs_mod, "redact_repo")

        def resolve_branch_to_sha(
            branch: str,
            *,
            repo: str,
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        ) -> str:
            cleaned = branch.strip()
            invalid_chars = " ~^:?*[\x5c"
            if (
                not cleaned
                or cleaned.startswith(("-", ".", "/", "refs/"))
                or cleaned.endswith((".", "/", ".lock"))
                or ".." in cleaned
                or "@{" in cleaned
                or "//" in cleaned
                or any(ord(char) < 32 or char in invalid_chars for char in cleaned)
            ):
                raise ValueError(f"invalid clone branch {branch!r}")
            remote_ref = f"refs/heads/{cleaned}"
            for sha, name in ls_remote_rows(repo, remote_ref, runner=runner):
                if name == remote_ref:
                    return sha
            raise ValueError(
                f"clone branch {branch!r} ({remote_ref}) not found in {redact_repo(repo)}"
            )

        setattr(refs_mod, "resolve_branch_to_sha", resolve_branch_to_sha)


_ensure_runner_sdk_compat()


def _load_module(file_path, module_name):
    """Load a Python file as a module"""
    if module_name in sys.modules:
        return sys.modules[module_name]
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
                    warning(f"Failed to add task {name}: {e}")


def _load_tasks_into_collection(
    file_path: Path,
    module_name: str,
    collection: Collection,
    sub_name: Optional[str] = None,
) -> bool:
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


def _load_project(ns, root, project_name, prefix=""):
    """Load all services from a project directory

    Args:
        ns: Root namespace collection
        root: Root directory path
        project_name: Name of the project (e.g., 'platform', 'finance_report')
        prefix: Optional task-name prefix (e.g. 'fr-') to avoid conflicts with
            platform services. Must match discover_services() in common/deploy/deployer.py.
    """
    project_dir = root / project_name
    if not project_dir.exists():
        return

    for comp_dir in sorted(project_dir.iterdir()):
        if not comp_dir.is_dir():
            continue

        name = comp_dir.name.split(".")[-1]
        # Prefix service name if needed (e.g., fr-postgres, ta-postgres)
        task_name = f"{prefix}{name}"
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
            ns.add_collection(coll, name=task_name)
            success(f"{project_name}/{name}")


def _load_tools(ns, root):
    """Load tools as a special project"""
    tools_dir = root / "tools"
    if not tools_dir.exists():
        return

    # Tools are loaded into isolated namespaces to avoid task name collisions.
    coll = Collection()
    if _load_tasks_into_collection(tools_dir / "env_tool.py", "tools.env_tool", coll):
        ns.add_collection(coll, name="env")
        success("tools/env")

    coll = Collection()
    if _load_tasks_into_collection(
        tools_dir / "dokploy_env.py", "tools.dokploy_env", coll
    ):
        ns.add_collection(coll, name="dokploy")
        success("tools/dokploy")

    coll = Collection()
    if _load_tasks_into_collection(
        tools_dir / "local_init.py", "tools.local_init", coll
    ):
        ns.add_collection(coll, name="local")
        success("tools/local")

    coll = Collection()
    if _load_tasks_into_collection(
        tools_dir / "vault_audit.py", "tools.vault_audit", coll
    ):
        ns.add_collection(coll, name="vault-audit")
        success("tools/vault-audit")

    coll = Collection()
    _load_tasks_into_collection(
        tools_dir / "service_onboard.py", "tools.service_onboard", coll
    )
    _load_tasks_into_collection(
        tools_dir / "service_offboard.py", "tools.service_offboard", coll
    )
    if coll.tasks:
        ns.add_collection(coll, name="service")
        success("tools/service")


def load_all():
    """Load all modules from all projects"""
    from common.core.environ import validate_env
    from invoke import task

    @task
    def check_env(c):
        """Check required environment variables"""
        missing = validate_env()
        if missing:
            error(f"Missing: {', '.join(missing)}")
            exit(1)
        success("Environment OK")

    ns = Collection()
    ns.add_task(check_env)

    root = Path(__file__).parent.parent

    # Load projects
    for project in ["bootstrap", "platform"]:
        _load_project(ns, root, project)

    # Load app layers (nested structure: <app>/<app>/)
    # Prefixes avoid conflicts with platform/postgres, platform/redis
    _load_project(ns, root / "finance_report", "finance_report", prefix="fr-")
    _load_project(ns, root / "truealpha", "truealpha", prefix="ta-")

    # Load tools
    _load_tools(ns, root)

    return ns


ns = load_all()
