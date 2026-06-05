#!/usr/bin/env python3
"""
Sync Runner - executes invoke sync tasks for changed services.
"""

import fcntl
import logging
import os
import re
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
MAX_RESULT_OUTPUT_CHARS = int(os.environ.get("MAX_RESULT_OUTPUT_CHARS", "4000"))

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
    "platform/alerting": "alerting.sync",
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
    "platform/alerting",
    "platform/portal",
    "platform/activepieces",
    "platform/prefect",
    "finance_report/postgres",
    "finance_report/redis",
    "finance_report/app",
]

_VAULT_ROOT_TOKEN_CACHE: str | None = None


def _tail_text(value: str, limit: int = MAX_RESULT_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"...<truncated {len(value) - limit} chars>...\n{value[-limit:]}"


def _first_matching_line(text: str, patterns: tuple[str, ...]) -> str:
    for line in text.splitlines():
        if any(pattern in line for pattern in patterns):
            return line.strip()
    return text.strip().splitlines()[-1].strip() if text.strip() else ""


def diagnose_failure(stderr: str, stdout: str = "") -> dict[str, str]:
    """Classify a failed invoke task into a one-look diagnostic."""
    combined = f"{stderr}\n{stdout}"
    missing_module_match = re.search(
        r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", combined
    )
    if missing_module_match:
        module_name = missing_module_match.group(1)
        return {
            "error_kind": "missing_python_dependency",
            "summary": f"IaC Runner runtime is missing Python module: {module_name}",
            "next_action": (
                "Rebuild or redeploy bootstrap/iac-runner from the current "
                "requirements.txt, then rerun the deployment."
            ),
        }

    if "Non-production requires DATA_PATH or ENV_SUFFIX" in combined:
        return {
            "error_kind": "missing_environment_isolation",
            "summary": "Child invoke task is missing DATA_PATH or ENV_SUFFIX for a non-production deploy.",
            "next_action": "Check IaC Runner child env: DEPLOY_ENV, ENV_SUFFIX, and ENV_DOMAIN_SUFFIX.",
        }

    secret_match = re.search(r"Secret not found:\s*([^\n]+)", combined)
    if secret_match:
        secret_path = secret_match.group(1).strip()
        return {
            "error_kind": "vault_secret_missing",
            "summary": f"Vault secret path is missing: {secret_path}",
            "next_action": f"Create or repair secret/data/{secret_path} before rerunning deploy.",
        }

    if "VAULT_ROOT_TOKEN not set" in combined:
        return {
            "error_kind": "vault_token_missing",
            "summary": "Child invoke task could not find VAULT_ROOT_TOKEN.",
            "next_action": "Check IaC Runner VAULT_APP_TOKEN rendering and token handoff to child tasks.",
        }

    if "permission denied" in combined.lower() and "vault" in combined.lower():
        return {
            "error_kind": "vault_permission_denied",
            "summary": "Vault rejected the token for the requested path.",
            "next_action": "Check the IaC Runner Vault policy and target env secret path.",
        }

    if "Timeout after" in combined:
        return {
            "error_kind": "invoke_timeout",
            "summary": _first_matching_line(combined, ("Timeout after",)),
            "next_action": "Inspect the service deploy logs and Dokploy deployment status for the timed-out task.",
        }

    summary = _first_matching_line(
        combined,
        (
            "Traceback",
            "Error:",
            "ERROR:",
            "ValueError:",
            "RuntimeError:",
            "Exception:",
        ),
    )
    return {
        "error_kind": "unknown_invoke_failure",
        "summary": summary or "Invoke task failed without a recognizable diagnostic.",
        "next_action": "Inspect this service stderr tail and the corresponding container/Dokploy logs.",
    }


@dataclass
class ServiceSyncResult:
    service: str
    task: str | None
    success: bool
    skipped: bool = False
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["stdout"] = _tail_text(self.stdout)
        data["stderr"] = _tail_text(self.stderr)
        if not self.success and not self.skipped:
            data["diagnostic"] = diagnose_failure(self.stderr, self.stdout)
        return data


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
        return sum(
            1 for result in self.results if result.success and not result.skipped
        )

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
        failure_summary = [
            {
                "service": result.service,
                "task": result.task,
                **diagnose_failure(result.stderr, result.stdout),
            }
            for result in self.results
            if not result.success
        ]
        return {
            "success": self.success,
            "env": self.env,
            "ref": self.ref,
            "requested_services": self.requested_services,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "error": self.error,
            "failure_summary": failure_summary,
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

    if token := env.get("VAULT_APP_TOKEN"):
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


def deploy_env_overrides(deploy_env: str) -> dict[str, str]:
    """Build deterministic environment isolation variables for invoke tasks."""
    if not isinstance(deploy_env, str):
        raise ValueError("deploy env must be a string")
    env_name = deploy_env.strip().lower()
    if not env_name:
        raise ValueError("deploy env must not be empty")
    if "-" in env_name or "/" in env_name:
        raise ValueError("deploy env name must not include '-' or '/' (use '_')")
    if env_name in ("prod", "production"):
        env_name = "production"

    env_dns = env_name.replace("_", "-")
    suffix = "" if env_name == "production" else f"-{env_dns}"
    return {
        "DEPLOY_ENV": env_name,
        "ENV_SUFFIX": suffix,
        "ENV_DOMAIN_SUFFIX": suffix,
    }


def safe_invoke_env_summary(env: dict[str, str]) -> str:
    """Summarize child env without logging secret values."""
    keys = [
        "DEPLOY_ENV",
        "ENV_SUFFIX",
        "ENV_DOMAIN_SUFFIX",
        "VAULT_ROOT_TOKEN",
        "VAULT_APP_TOKEN",
        "OP_SERVICE_ACCOUNT_TOKEN",
    ]
    parts = []
    for key in keys:
        value = env.get(key)
        if key.endswith("TOKEN"):
            value = "set" if value else "unset"
        parts.append(f"{key}={value if value is not None else 'unset'}")
    return " ".join(parts)


def run_invoke_task(
    task_name: str, repo_path: Path, deploy_env: str = "staging"
) -> dict:
    """Run an invoke task with timeout and return result."""
    logger.info(
        f"Running: invoke {task_name} (env={deploy_env}, timeout={DEPLOY_TIMEOUT}s)"
    )

    env_vars = {
        **os.environ,
        **deploy_env_overrides(deploy_env),
    }
    if vault_root_token := resolve_vault_root_token(env_vars):
        env_vars["VAULT_ROOT_TOKEN"] = vault_root_token
    logger.info("Invoke child env: %s", safe_invoke_env_summary(env_vars))

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
                diagnostic = diagnose_failure(
                    service_result.stderr, service_result.stdout
                )
                logger.error(
                    "Diagnostic: service=%s task=%s kind=%s summary=%s next=%s",
                    service,
                    task_name,
                    diagnostic["error_kind"],
                    diagnostic["summary"],
                    diagnostic["next_action"],
                )
                logger.error(_tail_text(service_result.stderr))

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
        for failure in sync_result.to_dict()["failure_summary"]:
            logger.error(
                "Failure summary: service=%s task=%s kind=%s summary=%s next=%s",
                failure["service"],
                failure["task"],
                failure["error_kind"],
                failure["summary"],
                failure["next_action"],
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
