#!/usr/bin/env python3
"""
Sync Runner - executes invoke sync tasks for changed services.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")
GIT_REPO_URL = os.environ.get("GIT_REPO_URL", "https://github.com/wangzitian0/infra2.git")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")

# Service name to invoke task mapping
# Maps "project/service" to invoke task name
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
    # Finance report services (use fr- prefix)
    "finance_report/postgres": "fr-postgres.sync",
    "finance_report/redis": "fr-redis.sync",
    "finance_report/app": "fr-app.sync",
    # Bootstrap services (typically not synced automatically)
    "bootstrap/vault": None,  # Skip - too risky
    "bootstrap/1password": None,  # Skip
    "bootstrap/iac-runner": None,  # Skip - would restart ourselves
}

# All syncable services for __all__ mode
ALL_SERVICES = [
    "platform/postgres",
    "platform/redis", 
    "platform/clickhouse",
    "platform/minio",
    "platform/authentik",
    "platform/signoz",
    "platform/portal",
    "platform/activepieces",
    "finance_report/postgres",
    "finance_report/redis",
    "finance_report/app",
]


def update_repo() -> bool:
    """Clone or update the repo."""
    repo_path = WORKSPACE / "infra2"
    
    if not repo_path.exists():
        logger.info(f"Cloning {GIT_REPO_URL} to {repo_path}")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "-b", GIT_BRANCH, GIT_REPO_URL, str(repo_path)],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logger.error(f"Clone failed: {result.stderr}")
            return False
    else:
        logger.info(f"Updating repo at {repo_path}")
        result = subprocess.run(
            ["git", "fetch", "origin", GIT_BRANCH],
            cwd=repo_path,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logger.error(f"Fetch failed: {result.stderr}")
            return False
        
        result = subprocess.run(
            ["git", "reset", "--hard", f"origin/{GIT_BRANCH}"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logger.error(f"Reset failed: {result.stderr}")
            return False
    
    logger.info("Repo updated successfully")
    return True


def run_invoke_task(task_name: str, repo_path: Path) -> dict:
    """Run an invoke task and return result."""
    logger.info(f"Running: invoke {task_name}")
    
    result = subprocess.run(
        ["invoke", task_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo_path)}
    )
    
    return {
        "task": task_name,
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr
    }


def sync_services(services: set[str]):
    """Sync the specified services."""
    logger.info(f"Starting sync for services: {services}")
    
    # Update repo first
    if not update_repo():
        logger.error("Failed to update repo, aborting sync")
        return
    
    repo_path = WORKSPACE / "infra2"
    
    # Expand __all__ to all services
    if "__all__" in services:
        services = set(ALL_SERVICES)
        logger.info("Syncing all services due to libs/ change")
    
    results = []
    for service in sorted(services):
        task_name = SERVICE_TASK_MAP.get(service)
        
        if task_name is None:
            logger.info(f"Skipping {service} (no sync task configured)")
            continue
        
        result = run_invoke_task(task_name, repo_path)
        results.append(result)
        
        if result["success"]:
            logger.info(f"✅ {service}: sync completed")
        else:
            logger.error(f"❌ {service}: sync failed")
            logger.error(result["stderr"])
    
    # Summary
    succeeded = sum(1 for r in results if r["success"])
    failed = sum(1 for r in results if not r["success"])
    logger.info(f"Sync complete: {succeeded} succeeded, {failed} failed")


if __name__ == "__main__":
    # CLI mode for testing
    if len(sys.argv) > 1:
        services = set(sys.argv[1:])
    else:
        services = {"__all__"}
    
    sync_services(services)
