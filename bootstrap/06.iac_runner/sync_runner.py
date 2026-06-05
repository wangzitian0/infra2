#!/usr/bin/env python3
"""
Sync Runner - executes invoke sync tasks for changed services.
"""

import fcntl
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")
GIT_REPO_URL = os.environ["GIT_REPO_URL"]
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")
DEPLOY_TIMEOUT = int(os.environ.get("DEPLOY_TIMEOUT", "600"))
DEFAULT_VAULT_ROOT_TOKEN_OP_REF = "op://Infra2/dexluuvzg5paff3cltmtnlnosm/Token"

REPO_NAME = Path(urlparse(GIT_REPO_URL).path).stem

WORKSPACE_LOCK_FILE = Path("/tmp/workspace.lock")
DEPLOYMENT_LOCK_FILE = Path("/tmp/deployment.lock")
INVOKE_BOOTSTRAP = (
    "import platform, runpy, sys; "
    "sys.path.insert(0, '.'); "
    "runpy.run_module('invoke', run_name='__main__')"
)

SERVICE_TASK_MAP = {
    "platform/postgres": "postgres.sync",
    "platform/redis": "redis.sync",
    "platform/clickhouse": "clickhouse.sync",
    "platform/minio": "minio.sync",
    "platform/authentik": "authentik.sync",
    "platform/signoz": "signoz.sync",
    "platform/portal": "portal.sync",
    "platform/activepieces": "activepieces.sync",
    "platform/prefect": "prefect.sync",
    "bootstrap/vault": None,
    "bootstrap/1password": None,
    "bootstrap/iac-runner": None,
    "finance_report/postgres": "fr-postgres.sync",
    "finance_report/redis": "fr-redis.sync",
    "finance_report/app": "fr-app.sync",
    "finance/wealthfolio": None,
}

ALL_SERVICES = [
    "platform/postgres",
    "platform/redis",
    "platform/clickhouse",
    "platform/minio",
    "platform/authentik",
    "platform/signoz",
    "platform/portal",
    "platform/activepieces",
    "platform/prefect",
    "finance_report/postgres",
    "finance_report/redis",
    "finance_report/app",
]

_VAULT_ROOT_TOKEN_CACHE: str | None = None


@dataclass
class ServiceSyncResult:
    service: str
    task: str | None
    success: bool
    skipped: bool = False
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SyncResult:
    env: str
    ref: str | None
    requested_services: list[str]
    results: list[ServiceSyncResult] = field(default_factory=list)
    repo_updated: bool = True
    error: str = ""

    @property
    def succeeded(self) -> int:
        return sum(1 for result in self.results if result.success and not result.skipped)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if not result.success)

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.skipped)

    @property
    def success(self) -> bool:
        return self.repo_updated and self.failed == 0 and not self.error

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "env": self.env,
            "ref": self.ref,
            "requested_services": self.requested_services,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "error": self.error,
            "results": [result.to_dict() for result in self.results],
        }


@contextmanager
def file_lock(lock_path: Path, description: str):
    """Acquire exclusive file lock to prevent concurrent operations."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    logger.info(f"Acquiring {description} lock...")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        logger.info(f"{description} lock acquired")
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
        logger.info(f"{description} lock released")


def run_git_command(args: list[str], repo_path: Path, description: str) -> bool:
    """Run a git command with proper error handling."""
    result = subprocess.run(
        ["git"] + args,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        logger.error(f"Git {description} failed: {result.stderr}")
        return False
    return True


def update_repo(ref: str | None = None) -> bool:
    """Clone or update the repo to a specific ref (branch/tag/commit)."""
    repo_path = WORKSPACE / REPO_NAME
    target_ref = ref if ref is not None else GIT_BRANCH

    with file_lock(WORKSPACE_LOCK_FILE, "workspace"):
        if not repo_path.exists():
            logger.info(f"Cloning {GIT_REPO_URL} to {repo_path}")
            result = subprocess.run(
                ["git", "clone", GIT_REPO_URL, str(repo_path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.error(f"Clone failed: {result.stderr}")
                return False

        logger.info(f"Checking out {target_ref}")

        if not run_git_command(["reset", "--hard", "HEAD"], repo_path, "reset"):
            return False

        if not run_git_command(["clean", "-fd"], repo_path, "clean"):
            return False

        if not run_git_command(
            ["fetch", "--tags", "--prune", "origin"], repo_path, "fetch"
        ):
            return False

        if not run_git_command(["checkout", target_ref], repo_path, "checkout"):
            return False

        if not run_git_command(
            ["reset", "--hard", target_ref], repo_path, "reset to target"
        ):
            return False

        logger.info(f"Repo checked out to {target_ref}")
        return True


def resolve_vault_root_token(env: dict[str, str]) -> str | None:
    """Resolve the Vault root token for infrastructure sync subprocesses."""
    global _VAULT_ROOT_TOKEN_CACHE

    if token := env.get("VAULT_ROOT_TOKEN"):
        return token

    if _VAULT_ROOT_TOKEN_CACHE:
        return _VAULT_ROOT_TOKEN_CACHE

    if not env.get("OP_SERVICE_ACCOUNT_TOKEN"):
        logger.warning("OP_SERVICE_ACCOUNT_TOKEN is not configured")
        return None

    op_ref = env.get("VAULT_ROOT_TOKEN_OP_REF") or DEFAULT_VAULT_ROOT_TOKEN_OP_REF
    result = subprocess.run(
        ["op", "read", op_ref],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if result.returncode != 0:
        logger.error("Failed to resolve Vault root token via 1Password")
        return None

    token = result.stdout.strip()
    if not token:
        logger.error("1Password returned an empty Vault root token")
        return None

    _VAULT_ROOT_TOKEN_CACHE = token
    return token


def run_invoke_task(
    task_name: str, repo_path: Path, deploy_env: str = "staging"
) -> dict:
    """Run an invoke task with timeout and return result."""
    logger.info(
        f"Running: invoke {task_name} (env={deploy_env}, timeout={DEPLOY_TIMEOUT}s)"
    )

    env_vars = {
        **os.environ,
        "DEPLOY_ENV": deploy_env,
    }
    if vault_root_token := resolve_vault_root_token(env_vars):
        env_vars["VAULT_ROOT_TOKEN"] = vault_root_token

    try:
        result = subprocess.run(
            [sys.executable, "-P", "-c", INVOKE_BOOTSTRAP, task_name],
            cwd=repo_path,
            capture_output=True,
            text=True,
            env=env_vars,
            timeout=DEPLOY_TIMEOUT,
        )
        return {
            "task": task_name,
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        logger.error(f"Task {task_name} timed out after {DEPLOY_TIMEOUT}s")
        return {
            "task": task_name,
            "success": False,
            "stdout": "",
            "stderr": f"Timeout after {DEPLOY_TIMEOUT}s",
        }


def sync_services(
    services: set[str], ref: str | None = None, deploy_env: str = "staging"
) -> SyncResult:
    """Sync the specified services with deployment lock."""
    requested_services = sorted(services)
    with file_lock(DEPLOYMENT_LOCK_FILE, "deployment"):
        logger.info(
            f"Starting sync for services: {services} (env={deploy_env}, ref={ref})"
        )

        if not update_repo(ref=ref):
            logger.error("Failed to update repo, aborting sync")
            return SyncResult(
                env=deploy_env,
                ref=ref,
                requested_services=requested_services,
                repo_updated=False,
                error="Failed to update repo",
            )

        repo_path = WORKSPACE / REPO_NAME

        if "__all__" in services:
            services = set(ALL_SERVICES)
            logger.info("Syncing all services due to libs/ change")

        results: list[ServiceSyncResult] = []
        for service in sorted(services):
            task_name = SERVICE_TASK_MAP.get(service)

            if task_name is None:
                logger.info(f"Skipping {service} (no sync task configured)")
                results.append(
                    ServiceSyncResult(
                        service=service,
                        task=None,
                        success=True,
                        skipped=True,
                    )
                )
                continue

            result = run_invoke_task(task_name, repo_path, deploy_env)
            service_result = ServiceSyncResult(
                service=service,
                task=task_name,
                success=bool(result["success"]),
                stdout=str(result.get("stdout", "")),
                stderr=str(result.get("stderr", "")),
            )
            results.append(service_result)

            if service_result.success:
                logger.info(f"✅ {service}: sync completed")
            else:
                logger.error(f"❌ {service}: sync failed")
                logger.error(service_result.stderr)

        sync_result = SyncResult(
            env=deploy_env,
            ref=ref,
            requested_services=requested_services,
            results=results,
        )
        logger.info(
            "Sync complete: "
            f"{sync_result.succeeded} succeeded, "
            f"{sync_result.failed} failed, "
            f"{sync_result.skipped} skipped"
        )
        return sync_result


def sync_services_by_version(env: str, ref: str, triggered_by: str) -> SyncResult:
    """Deploy all platform services to a specific environment using a git ref."""
    logger.info("=== Deployment Started ===")
    logger.info(f"Environment: {env}")
    logger.info(f"Ref: {ref}")
    logger.info(f"Triggered by: {triggered_by}")

    result = sync_services(set(ALL_SERVICES), ref=ref, deploy_env=env)

    logger.info(
        "=== Deployment Complete: "
        f"{'success' if result.success else 'failed'} "
        f"({result.succeeded} succeeded, {result.failed} failed) ==="
    )
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1:
        services = set(sys.argv[1:])
    else:
        services = {"__all__"}

    sync_services(services)
