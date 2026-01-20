#!/usr/bin/env python3
"""
Sync Runner - executes invoke sync tasks for changed services.
"""

import fcntl
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
LOCK_FILE = WORKSPACE / ".sync.lock"
GIT_REPO_URL = os.environ.get("GIT_REPO_URL")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # For commit status updates
GITHUB_REPO = "wangzitian0/infra2"  # TODO: Make configurable

if not GIT_REPO_URL:
    raise RuntimeError("GIT_REPO_URL environment variable must be set")

# Extract repo name from URL (e.g., "infra2" from "https://github.com/user/infra2.git")
REPO_NAME = Path(urlparse(GIT_REPO_URL).path).stem


# Service discovery: dynamically load from libs.deployer
def get_service_task_map() -> dict[str, str | None]:
    """Get service task mapping, combining auto-discovery with manual exclusions."""
    try:
        sys.path.insert(0, str(WORKSPACE / REPO_NAME))
        from libs.deployer import discover_services

        services = discover_services()

        # Add manual exclusions for high-risk services
        services.update(
            {
                "bootstrap/vault": None,  # Skip - too risky
                "bootstrap/1password": None,  # Skip
                "bootstrap/iac-runner": None,  # Skip - would restart ourselves
            }
        )

        return services
    except Exception as e:
        logger.warning(f"Failed to auto-discover services: {e}")
        # Fallback to hardcoded map
        return {
            "platform/postgres": "postgres.sync",
            "platform/redis": "redis.sync",
            "platform/clickhouse": "clickhouse.sync",
            "platform/minio": "minio.sync",
            "platform/authentik": "authentik.sync",
            "platform/signoz": "signoz.sync",
            "platform/portal": "portal.sync",
            "platform/activepieces": "activepieces.sync",
            "finance_report/postgres": "fr-postgres.sync",
            "finance_report/redis": "fr-redis.sync",
            "finance_report/app": "fr-app.sync",
            "bootstrap/vault": None,
            "bootstrap/1password": None,
            "bootstrap/iac-runner": None,
        }


def update_github_status(
    commit_sha: str | None,
    state: str,
    description: str,
    context: str = "IaC Runner / Sync",
):
    """Update GitHub commit status API."""
    if not GITHUB_TOKEN or not commit_sha:
        logger.debug("GITHUB_TOKEN or commit_sha not available, skipping status update")
        return

    try:
        import httpx

        url = f"https://api.github.com/repos/{GITHUB_REPO}/statuses/{commit_sha}"
        httpx.post(
            url,
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={
                "state": state,  # pending, success, failure, error
                "context": context,
                "description": description[:140],  # GitHub limit
            },
            timeout=10,
        )
        logger.info(f"GitHub status updated: {state} - {description}")
    except Exception as e:
        logger.warning(f"Failed to update GitHub status: {e}")


def update_repo() -> bool:
    """Clone or update the repo."""
    repo_path = WORKSPACE / REPO_NAME

    if not repo_path.exists():
        logger.info(f"Cloning {GIT_REPO_URL} to {repo_path}")
        result = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "-b",
                GIT_BRANCH,
                GIT_REPO_URL,
                str(repo_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Clone failed: {result.stderr}")
            return False
    else:
        logger.info(f"Updating repo at {repo_path}")
        # Clean any local changes first
        subprocess.run(["git", "clean", "-fd"], cwd=repo_path, capture_output=True)

        result = subprocess.run(
            ["git", "fetch", "origin", GIT_BRANCH],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Fetch failed: {result.stderr}")
            return False

        result = subprocess.run(
            ["git", "reset", "--hard", f"origin/{GIT_BRANCH}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
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
        env={**os.environ, "PYTHONPATH": str(repo_path)},
    )

    return {
        "task": task_name,
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def sync_services(services: set[str], commit_sha: str | None = None):
    """Sync the specified services with file-based locking."""
    logger.info(f"Starting sync for services: {services}")

    # Acquire lock to prevent concurrent syncs
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)

    with open(LOCK_FILE, "w") as lock:
        try:
            # Try non-blocking lock first
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            logger.info("Lock acquired, proceeding with sync")
        except BlockingIOError:
            logger.warning("Another sync in progress, waiting for lock...")
            update_github_status(
                commit_sha, "pending", "Waiting for previous sync to complete"
            )
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # Wait for lock
            logger.info("Lock acquired after waiting")

        # Update status to pending
        update_github_status(
            commit_sha, "pending", f"Syncing {len(services)} service(s)..."
        )

        # Update repo first
        if not update_repo():
            logger.error("Failed to update repo, aborting sync")
            update_github_status(commit_sha, "error", "Failed to update git repository")
            return

        repo_path = WORKSPACE / REPO_NAME

        # Get service task map (now with auto-discovery)
        service_task_map = get_service_task_map()

        # Expand __all__ to all discovered services
        if "__all__" in services:
            # Get all services except those mapped to None
            services = {k for k, v in service_task_map.items() if v is not None}
            logger.info(
                f"Syncing all services due to libs/ change: {len(services)} services"
            )

        results = []
        for service in sorted(services):
            task_name = service_task_map.get(service)

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

        # Update GitHub status
        if failed > 0:
            update_github_status(
                commit_sha,
                "failure",
                f"{failed}/{len(results)} service(s) failed to sync",
            )
        elif succeeded > 0:
            update_github_status(
                commit_sha, "success", f"{succeeded} service(s) synced successfully"
            )
        else:
            update_github_status(commit_sha, "success", "No services required sync")


if __name__ == "__main__":
    # CLI mode for testing
    if len(sys.argv) > 1:
        services = set(sys.argv[1:])
    else:
        services = {"__all__"}

    sync_services(services)
