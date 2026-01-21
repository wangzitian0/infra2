#!/usr/bin/env python3
"""
Sync Runner - executes invoke sync tasks for changed services.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")
GIT_REPO_URL = os.environ["GIT_REPO_URL"]  # Required - will raise KeyError if missing
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")

# Extract repo name from URL (e.g., "infra2" from "https://github.com/user/infra2.git")
REPO_NAME = Path(urlparse(GIT_REPO_URL).path).stem

# Service name to invoke task mapping
# Maps "project/service" to invoke task name
# NOTE: IaC Runner ONLY manages platform services
# - Bootstrap: Manual deployment (avoid circular deps)
# - Platform: Auto-sync via IaC Runner
# - Apps (finance_report, wealthfolio): Own CI/CD pipelines
SERVICE_TASK_MAP = {
    # Platform services
    "platform/postgres": "postgres.sync",
    "platform/redis": "redis.sync",
    "platform/clickhouse": "clickhouse.sync",
    "platform/minio": "minio.sync",
    "platform/authentik": "authentik.sync",
    "platform/signoz": "signoz.sync",
    "platform/portal": "portal.sync",
    "platform/activepieces": "activepieces.sync",
    # Bootstrap services (manual only)
    "bootstrap/vault": None,  # Skip - too risky
    "bootstrap/1password": None,  # Skip
    "bootstrap/iac-runner": None,  # Skip - would restart ourselves
    # App services (use their own CI/CD)
    "finance_report/postgres": None,  # Skip - use finance_report CI
    "finance_report/redis": None,  # Skip - use finance_report CI
    "finance_report/app": None,  # Skip - use finance_report CI
    "finance/wealthfolio": None,  # Skip - use wealthfolio CI
}

# All syncable services for __all__ mode (ONLY platform services)
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


def update_repo(ref: str | None = None) -> bool:
    """Clone or update the repo to a specific ref (branch/tag/commit).

    Args:
        ref: Git ref to checkout (tag, commit SHA, or branch). If None, uses GIT_BRANCH.
    """
    repo_path = WORKSPACE / REPO_NAME
    target_ref = ref if ref is not None else GIT_BRANCH

    if not repo_path.exists():
        logger.info(f"Cloning {GIT_REPO_URL} to {repo_path}")
        result = subprocess.run(
            ["git", "clone", GIT_REPO_URL, str(repo_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Clone failed: {result.stderr}")
            return False

    logger.info(f"Checking out {target_ref}")
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"], cwd=repo_path, capture_output=True
    )
    subprocess.run(["git", "clean", "-fd"], cwd=repo_path, capture_output=True)
    subprocess.run(
        ["git", "fetch", "--tags", "origin"], cwd=repo_path, capture_output=True
    )

    result = subprocess.run(
        ["git", "checkout", target_ref],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"Checkout failed: {result.stderr}")
        return False

    result = subprocess.run(
        ["git", "reset", "--hard", target_ref],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"Reset failed: {result.stderr}")
        return False

    logger.info(f"Repo checked out to {target_ref}")
    return True


def run_invoke_task(
    task_name: str, repo_path: Path, deploy_env: str = "staging"
) -> dict:
    """Run an invoke task and return result."""
    logger.info(f"Running: invoke {task_name} (env={deploy_env})")

    env_vars = {
        **os.environ,
        "PYTHONPATH": str(repo_path),
        "DEPLOY_ENV": deploy_env,
    }

    result = subprocess.run(
        ["invoke", task_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=env_vars,
    )

    return {
        "task": task_name,
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def sync_services(
    services: set[str], ref: str | None = None, deploy_env: str = "staging"
):
    """Sync the specified services.

    Args:
        services: Set of service identifiers to sync
        ref: Git ref (tag/commit/branch) to deploy
        deploy_env: Target environment (staging/production)
    """
    logger.info(f"Starting sync for services: {services} (env={deploy_env}, ref={ref})")

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
    """Deploy all platform services to a specific environment using a version tag.

    This is the entry point for GitOps-driven deployments.
    """
    logger.info(f"=== Version Deployment Started ===")
    logger.info(f"Environment: {env}")
    logger.info(f"Tag: {tag}")
    logger.info(f"Triggered by: {triggered_by}")

    sync_services(set(ALL_SERVICES), ref=tag, deploy_env=env)

    logger.info(f"=== Version Deployment Complete ===")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        services = set(sys.argv[1:])
    else:
        services = {"__all__"}

    sync_services(services)
