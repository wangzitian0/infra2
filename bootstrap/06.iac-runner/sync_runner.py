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

REPO_NAME = Path(urlparse(GIT_REPO_URL).path).stem

WORKSPACE_LOCK_FILE = Path("/tmp/workspace.lock")
DEPLOYMENT_LOCK_FILE = Path("/tmp/deployment.lock")

SERVICE_TASK_MAP = {
    "platform/postgres": "postgres.sync",
    "platform/redis": "redis.sync",
    "platform/clickhouse": "clickhouse.sync",
    "platform/minio": "minio.sync",
    "platform/authentik": "authentik.sync",
    "platform/signoz": "signoz.sync",
    "platform/portal": "portal.sync",
    "platform/activepieces": "activepieces.sync",
    "bootstrap/vault": None,
    "bootstrap/1password": None,
    "bootstrap/iac-runner": None,
    "finance_report/postgres": None,
    "finance_report/redis": None,
    "finance_report/app": None,
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
]


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


def run_invoke_task(
    task_name: str, repo_path: Path, deploy_env: str = "staging"
) -> dict:
    """Run an invoke task with timeout and return result."""
    logger.info(
        f"Running: invoke {task_name} (env={deploy_env}, timeout={DEPLOY_TIMEOUT}s)"
    )

    env_vars = {
        **os.environ,
        "PYTHONPATH": str(repo_path),
        "DEPLOY_ENV": deploy_env,
    }

    try:
        result = subprocess.run(
            ["invoke", task_name],
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
):
    """Sync the specified services with deployment lock."""
    with file_lock(DEPLOYMENT_LOCK_FILE, "deployment"):
        logger.info(
            f"Starting sync for services: {services} (env={deploy_env}, ref={ref})"
        )

        if not update_repo(ref=ref):
            logger.error("Failed to update repo, aborting sync")
            return

        repo_path = WORKSPACE / REPO_NAME

        if "__all__" in services:
            services = set(ALL_SERVICES)
            logger.info("Syncing all services due to libs/ change")

        results = []
        for service in sorted(services):
            task_name = SERVICE_TASK_MAP.get(service)

            if task_name is None:
                logger.info(f"Skipping {service} (no sync task configured)")
                continue

            result = run_invoke_task(task_name, repo_path, deploy_env)
            results.append(result)

            if result["success"]:
                logger.info(f"✅ {service}: sync completed")
            else:
                logger.error(f"❌ {service}: sync failed")
                logger.error(result["stderr"])

        succeeded = sum(1 for r in results if r["success"])
        failed = sum(1 for r in results if not r["success"])
        logger.info(f"Sync complete: {succeeded} succeeded, {failed} failed")


def sync_services_by_version(env: str, tag: str, triggered_by: str):
    """Deploy all platform services to a specific environment using a version tag."""
    logger.info("=== Version Deployment Started ===")
    logger.info(f"Environment: {env}")
    logger.info(f"Tag: {tag}")
    logger.info(f"Triggered by: {triggered_by}")

    sync_services(set(ALL_SERVICES), ref=tag, deploy_env=env)

    logger.info("=== Version Deployment Complete ===")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        services = set(sys.argv[1:])
    else:
        services = {"__all__"}

    sync_services(services)
