#!/usr/bin/env python3
"""
Sync Runner - executes invoke sync tasks for changed services.
"""

import fcntl
import json
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
MAX_RESULT_OUTPUT_CHARS = int(os.environ.get("MAX_RESULT_OUTPUT_CHARS", "4000"))
EXACT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")

REPO_NAME = Path(urlparse(GIT_REPO_URL).path).stem

# The iac-runner image bakes only this directory (build context = here), so
# `libs/` is NOT importable from /app. The dependency matcher + manifest live in
# the checked-out repo; put it on the path so the lazy
# `from libs.deploy_dependencies import ...` calls resolve (after update_repo()).
_CHECKOUT_PATH = str(WORKSPACE / REPO_NAME)
if _CHECKOUT_PATH not in sys.path:
    sys.path.insert(0, _CHECKOUT_PATH)

WORKSPACE_LOCK_FILE = Path("/tmp/workspace.lock")
DEPLOYMENT_LOCK_FILE = Path("/tmp/deployment.lock")
INVOKE_BOOTSTRAP = (
    "import platform, runpy, sys; "
    "sys.path.insert(0, '.'); "
    "runpy.run_module('invoke', run_name='__main__')"
)

# The bootstrap/* layer has no deploy.py (it's not a deploy.py-driven service), so its
# explicit "no sync task" entries are stated here; everything else is DERIVED.
_BOOTSTRAP_TASKS = {
    "bootstrap/vault": None,
    "bootstrap/1password": None,
    "bootstrap/iac-runner": None,
}


def _service_task_map() -> dict[str, "str | None"]:
    """service_id -> invoke sync task, DERIVED from libs.deploy.deployer.discover_services (the single
    source) instead of a hand-maintained parallel list (Infra-013, same as deploy_contract).

    Lazy import: in the iac-runner image `libs/` is on sys.path only AFTER update_repo() checks
    the repo out, and every caller runs post-checkout, so this resolves at call time.
    """
    from libs.deploy.deployer import discover_services

    return {**discover_services(), **_BOOTSTRAP_TASKS}


def _all_services() -> list[str]:
    """Every deployable service_id (= libs.deploy.deployer.discover_services keys), sorted."""
    from libs.deploy.deployer import discover_services

    return sorted(discover_services())


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
            "next_action": (
                "Check the IaC Runner AppRole login: VAULT_ROLE_ID/VAULT_SECRET_ID "
                "must be present in the runner env and `vault write auth/approle/login` "
                "must succeed (re-run setup-approle if the secret_id was wiped)."
            ),
        }

    # Dokploy auth must be checked BEFORE the generic Vault rule below: a failed
    # Dokploy API call surfaces as "No GitHub provider found" (the provider lookup
    # silently swallows the 401), while unrelated "permission denied"/"vault"
    # strings elsewhere in the output would otherwise mis-route this to
    # vault_permission_denied — exactly the red herring that masked an empty
    # DOKPLOY_API_KEY on the runner.
    lower = combined.lower()
    if (
        "no github provider" in lower
        or "no git provider" in lower
        or ("dokploy" in lower and ("unauthorized" in lower or "401" in lower))
    ):
        return {
            "error_kind": "dokploy_auth_failed",
            "summary": (
                "Dokploy deploy could not resolve a GitHub provider — either an "
                "empty/invalid DOKPLOY_API_KEY or no GitHub provider configured."
            ),
            "next_action": (
                "Check in order: (1) the runner's DOKPLOY_API_KEY is populated — its "
                "Dokploy compose env can be wiped on redeploy; (2) a GitHub provider "
                "exists in Dokploy (github.githubProviders). Fix whichever is missing "
                "and redeploy the runner."
            ),
        }

    if "permission denied" in lower and "vault" in lower:
        return {
            "error_kind": "vault_permission_denied",
            "summary": "Vault rejected the token for the requested path.",
            "next_action": "Check the IaC Runner Vault policy and target env secret path.",
        }

    if "Service Account" in combined or (
        "1Password" in combined and "sync failed" in combined
    ):
        return {
            "error_kind": "onepassword_auth_failed",
            "summary": _first_matching_line(
                combined, ("Service Account", "1Password to Vault sync failed")
            ),
            "next_action": (
                "OP_SERVICE_ACCOUNT_TOKEN is invalid (e.g. the 1Password service "
                "account was deleted). Recreate the service account, update the "
                "iac-runner's OP_SERVICE_ACCOUNT_TOKEN secret, and redeploy it."
            ),
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

    def to_public_dict(self) -> dict:
        data = {
            "service": self.service,
            "task": self.task,
            "success": self.success,
            "skipped": self.skipped,
        }
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

    def to_public_dict(self) -> dict:
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
            "results": [result.to_public_dict() for result in self.results],
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


def resolve_checkout_ref(repo_path: Path, target_ref: str) -> str | None:
    """Resolve a deploy ref to an immutable commit SHA after fetch."""
    candidates = []
    if EXACT_COMMIT_RE.fullmatch(target_ref):
        candidates.append(target_ref)
    else:
        candidates.extend(
            [
                f"refs/remotes/origin/{target_ref}^{{commit}}",
                f"refs/tags/{target_ref}^{{commit}}",
                f"{target_ref}^{{commit}}",
            ]
        )

    for candidate in candidates:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return None


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

        checkout_ref = resolve_checkout_ref(repo_path, target_ref)
        if not checkout_ref:
            logger.error("Unable to resolve deploy ref to a commit: %s", target_ref)
            return False

        if not run_git_command(
            ["checkout", "--detach", checkout_ref], repo_path, "checkout"
        ):
            return False

        if not run_git_command(
            ["reset", "--hard", checkout_ref], repo_path, "reset to target"
        ):
            return False

        logger.info("Repo checked out to %s (%s)", target_ref, checkout_ref[:12])
        return True


def _approle_login(env: dict[str, str], role_id: str, secret_id: str) -> str | None:
    """Exchange AppRole role_id/secret_id for a short-TTL bounded Vault token.

    secret_id is passed on stdin (`secret_id=-`), never argv, so it cannot leak
    via the process table. VAULT_TOKEN is stripped so the login is unauthenticated.
    """
    if not env.get("VAULT_ADDR"):
        logger.error("VAULT_ADDR not set; cannot perform AppRole login")
        return None

    login_env = {key: value for key, value in env.items() if key != "VAULT_TOKEN"}
    try:
        result = subprocess.run(
            [
                "vault",
                "write",
                "-format=json",
                "auth/approle/login",
                f"role_id={role_id}",
                "secret_id=-",
            ],
            input=secret_id,
            capture_output=True,
            text=True,
            env=login_env,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("AppRole login subprocess failed: %s", exc)
        return None

    if result.returncode != 0:
        logger.error("AppRole login failed: %s", result.stderr.strip())
        return None

    try:
        token = json.loads(result.stdout)["auth"]["client_token"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("AppRole login returned an unparseable response: %s", exc)
        return None

    logger.info("AppRole login succeeded; bounded deploy token acquired")
    return token


def resolve_vault_root_token(env: dict[str, str]) -> str | None:
    """Resolve the Vault token for infrastructure sync subprocesses.

    Order:
      1. An explicit VAULT_ROOT_TOKEN (operator/manual override; tests).
      2. AppRole login with the Dokploy-injected VAULT_ROLE_ID/VAULT_SECRET_ID,
         yielding a short-TTL bounded token (no delete, no root — see
         bootstrap/06.iac_runner/vault-policy.hcl).

    The legacy static VAULT_APP_TOKEN fallback is removed: iac-runner is on
    AppRole (docs/ssot/bootstrap.iac_runner.md §6.4). The credential is never read
    from 1Password — role_id/secret_id come from Dokploy env, not `op read`.
    """
    if token := env.get("VAULT_ROOT_TOKEN"):
        return token

    role_id = env.get("VAULT_ROLE_ID")
    secret_id = env.get("VAULT_SECRET_ID")
    if role_id and secret_id:
        return _approle_login(env, role_id, secret_id)

    logger.warning(
        "No VAULT_ROOT_TOKEN and no VAULT_ROLE_ID/VAULT_SECRET_ID for AppRole login"
    )
    return None


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


def resolve_checkout_head(repo_path: Path) -> str | None:
    """Resolve an exact checkout identity without raising on an absent workspace."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return None
    value = result.stdout.strip().lower() if result.returncode == 0 else ""
    return value if EXACT_COMMIT_RE.fullmatch(value) else None


def safe_invoke_env_summary(env: dict[str, str]) -> str:
    """Summarize child env without logging secret values."""
    keys = [
        "DEPLOY_ENV",
        "ENV_SUFFIX",
        "ENV_DOMAIN_SUFFIX",
        "IAC_DEPLOY_REF",
        "VAULT_ROOT_TOKEN",
        "VAULT_ROLE_ID",
        "VAULT_SECRET_ID",
        "OP_SERVICE_ACCOUNT_TOKEN",
    ]
    # role_id/secret_id are secret-equivalent (the AppRole login material), so
    # mask them alongside tokens — never print their values.
    secret_suffixes = ("TOKEN", "ROLE_ID", "SECRET_ID")
    parts = []
    for key in keys:
        value = env.get(key)
        if key.endswith(secret_suffixes):
            value = "set" if value else "unset"
        parts.append(f"{key}={value if value is not None else 'unset'}")
    return " ".join(parts)


def run_invoke_task(
    task_name: str,
    repo_path: Path,
    deploy_env: str = "staging",
    deploy_ref: str | None = None,
) -> dict:
    """Run an invoke task with timeout and return result."""
    logger.info(
        f"Running: invoke {task_name} (env={deploy_env}, timeout={DEPLOY_TIMEOUT}s)"
    )

    env_vars = {
        **os.environ,
        **deploy_env_overrides(deploy_env),
    }
    if deploy_ref:
        if not EXACT_COMMIT_RE.fullmatch(deploy_ref):
            raise ValueError("deploy_ref must be an exact 40-character commit SHA")
        env_vars["IAC_DEPLOY_REF"] = deploy_ref.lower()
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


def get_changed_services_from_files(changed_files: list[str]) -> set[str]:
    """Map changed files to affected services via the deploy dependency graph.

    Affected = a changed file is under the service's own directory OR matches a
    declared extra dependency (docs/ssot/deploy-dependencies.yaml). Deploy
    tooling such as libs/ and tools/ fans out to NOTHING — this replaces the old
    `libs/ -> __all__` catch-all that redeployed every service on any
    shared-tooling change (the over-fan-out behind the recurring mass redeploys).
    """
    try:
        from libs.deploy_dependencies import match_changed_services

        return match_changed_services(changed_files)
    except Exception as exc:  # checked-out libs/ not importable yet
        logger.warning(
            "deploy_dependencies unavailable (%s); own-dir-only fan-out", exc
        )
        return _own_dir_services(changed_files)


def _own_dir_services(changed_files: list[str]) -> set[str]:
    """Self-contained own-directory fan-out (no manifest, no catch-all) used as a
    safe fallback when the checked-out libs/ is not importable. libs/ and tools/
    map to nothing."""
    services: set[str] = set()
    for file_path in changed_files:
        parts = file_path.split("/")
        if parts[0] == "platform" and len(parts) >= 2 and "." in parts[1]:
            services.add(f"platform/{parts[1].split('.', 1)[1]}")
        elif parts[0] == "finance_report" and len(parts) >= 3 and "." in parts[2]:
            services.add(f"finance_report/{parts[2].split('.', 1)[1]}")
        elif parts[0] == "bootstrap" and len(parts) >= 2 and "." in parts[1]:
            services.add(f"bootstrap/{parts[1].split('.', 1)[1].replace('_', '-')}")
    return services


def _log_fanout_decision(changed_files: list[str], services: set[str]) -> None:
    """Log WHY each service was selected and which changed files fanned out to
    nothing, so a no-op deploy is debuggable (correctly-skipped vs under-deployed).
    """
    try:
        from libs.deploy_dependencies import explain_fanout

        decision = explain_fanout(changed_files)
    except Exception as exc:  # logging is best-effort; never fail the sync
        logger.warning("fan-out explain unavailable (%s)", exc)
        return
    logger.info(
        "Fan-out: %d changed file(s) -> %d service(s): %s",
        len(changed_files),
        len(decision.selected),
        services,
    )
    for service, reason in sorted(decision.selected.items()):
        logger.info("  selected %s: %s", service, reason)
    if decision.dropped:
        # Expected for pure tooling/shared changes; surfaced so an UNEXPECTED
        # drop (a service that should have been baked in) is visible in the log.
        logger.info(
            "  %d changed file(s) fanned out to nothing (tooling/shared): %s",
            len(decision.dropped),
            ", ".join(sorted(decision.dropped)),
        )


def sync_services(
    services: set[str], ref: str | None = None, deploy_env: str = "staging"
) -> SyncResult:
    """Sync the specified services with deployment lock."""
    requested_services = sorted(services)
    with file_lock(DEPLOYMENT_LOCK_FILE, "deployment"):
        logger.info(
            f"Starting sync for services: {services} (env={deploy_env}, ref={ref})"
        )

        repo_path = WORKSPACE / REPO_NAME
        current_head = resolve_checkout_head(repo_path) if repo_path.exists() else None

        if not update_repo(ref=ref):
            logger.error("Failed to update repo, aborting sync")
            return SyncResult(
                env=deploy_env,
                ref=ref,
                requested_services=requested_services,
                repo_updated=False,
                error="Failed to update repo",
            )

        resolved_head = resolve_checkout_head(repo_path)
        if ref and EXACT_COMMIT_RE.fullmatch(ref):
            if resolved_head != ref.lower():
                logger.error(
                    "Checked-out HEAD does not match requested ref: expected=%s actual=%s",
                    ref.lower(),
                    resolved_head or "unavailable",
                )
                return SyncResult(
                    env=deploy_env,
                    ref=ref,
                    requested_services=requested_services,
                    repo_updated=False,
                    error="Checked-out HEAD does not match requested exact ref",
                )

        if "__all__" in services:
            services = set(_all_services())
            logger.info("Syncing all services due to libs/ change")
        elif not services:
            # If services set is empty, determine changes dynamically via git diff
            if current_head and resolved_head and current_head != resolved_head:
                res_diff = subprocess.run(
                    ["git", "diff", "--name-only", current_head, resolved_head],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if res_diff.returncode == 0:
                    changed_files = res_diff.stdout.splitlines()
                    services = get_changed_services_from_files(changed_files)
                    _log_fanout_decision(changed_files, services)
                    if "__all__" in services:
                        services = set(_all_services())

            if not services:
                # If still empty (fresh clone or no detected changes), fallback to all
                services = set(_all_services())
                logger.info("No changes detected or fresh clone; syncing all services")

        results: list[ServiceSyncResult] = []
        task_map = (
            _service_task_map()
        )  # discovered once; reused for every service below
        for service in sorted(services):
            task_name = task_map.get(service)

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

            if resolved_head:
                result = run_invoke_task(
                    task_name, repo_path, deploy_env, resolved_head
                )
            else:
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


def sync_services_by_version(
    env: str, ref: str, triggered_by: str, services: list[str] | None = None
) -> SyncResult:
    """Deploy targeted platform services to a specific environment using a git ref."""
    logger.info("=== Deployment Started ===")
    logger.info(f"Environment: {env}")
    logger.info(f"Ref: {ref}")
    logger.info(f"Triggered by: {triggered_by}")
    if services:
        logger.info(f"Targeted Services: {services}")

    target_services = set(services) if services else set()
    result = sync_services(target_services, ref=ref, deploy_env=env)

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
