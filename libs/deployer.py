"""Base deployer with DRY task generation

Simplified: minimal class attributes, uses new env.py API.
"""

from __future__ import annotations
from typing import TYPE_CHECKING, Any
from pathlib import Path
import os
import hashlib
import json
import shlex
import time
from invoke import task

from libs.common import get_env, validate_env, service_domain
from libs.console import (
    header,
    success,
    error,
    warning,
    info,
    env_vars,
    run_with_status,
)
from libs.env import VaultSecrets, generate_password, get_secrets, verify_vault_token

if TYPE_CHECKING:
    from invoke import Context


__all__ = ["Deployer", "make_tasks", "discover_services"]

RUNTIME_ENV_KEYS_TO_PRESERVE = {"VAULT_APP_TOKEN"}


def discover_services() -> dict[str, str]:
    """Discover deployable services based on deploy.py files."""
    root = Path(__file__).resolve().parents[1]
    service_map: dict[str, str] = {}

    layers = {
        "platform": root / "platform",
        "finance_report": root / "finance_report" / "finance_report",
    }

    for layer, layer_path in layers.items():
        if not layer_path.exists():
            continue

        for service_dir in layer_path.iterdir():
            if not service_dir.is_dir():
                continue

            parts = service_dir.name.split(".", 1)
            if len(parts) != 2:
                continue

            service_name = parts[1]
            deploy_file = service_dir / "deploy.py"
            if not deploy_file.exists():
                continue

            key = f"{layer}/{service_name}"
            task_prefix = "fr-" if layer == "finance_report" else ""
            service_map[key] = f"{task_prefix}{service_name}.sync"

    return service_map


def _compute_config_hash(
    compose_content: str,
    env_vars: dict[str, str],
    artifact_payload: str = "",
) -> str:
    """Compute hash of compose content + env vars for change detection."""
    # Normalize env vars (sort keys, ignore empty values)
    env_str = "\n".join(f"{k}={v}" for k, v in sorted(env_vars.items()) if v)
    combined = (
        f"{compose_content}\n---ENV---\n{env_str}\n---ARTIFACTS---\n{artifact_payload}"
    )
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


def _iter_path_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(
        child
        for child in path.rglob("*")
        if child.is_file()
        and "__pycache__" not in child.parts
        and not child.name.endswith((".pyc", ".pyo"))
    )


def _resolve_compose_relative(compose_dir: Path, source: str) -> Path | None:
    if not source or "${" in source or source.startswith("/"):
        return None
    return (compose_dir / source).resolve()


def _resolve_build_relative(context_dir: Path, source: str) -> Path | None:
    if (
        not source
        or "${" in source
        or source.startswith("/")
        or source.startswith("--")
    ):
        return None
    return (context_dir / source).resolve()


def _dockerfile_copy_sources(dockerfile: Path, context_dir: Path) -> list[Path]:
    if not dockerfile.exists():
        return []

    sources: list[Path] = []
    for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        instruction, _, remainder = line.partition(" ")
        if instruction.upper() not in {"COPY", "ADD"} or not remainder:
            continue
        if "--from=" in remainder:
            continue

        parsed_sources: list[str] = []
        if remainder.startswith("["):
            try:
                values = json.loads(remainder)
            except json.JSONDecodeError:
                values = []
            if isinstance(values, list) and len(values) >= 2:
                parsed_sources = [str(value) for value in values[:-1]]
        else:
            parts = [
                part for part in shlex.split(remainder) if not part.startswith("--")
            ]
            if len(parts) >= 2:
                parsed_sources = parts[:-1]

        for source in parsed_sources:
            resolved = _resolve_build_relative(context_dir, source)
            if resolved:
                sources.extend(_iter_path_files(resolved))

    return sources


def _compose_artifact_files(compose_path: str, compose_content: str) -> list[Path]:
    try:
        import yaml
    except ModuleNotFoundError:
        return []

    compose_file = Path(compose_path)
    compose_dir = compose_file.parent.resolve()
    try:
        compose = yaml.safe_load(compose_content) or {}
    except yaml.YAMLError:
        return []

    services = compose.get("services", {})
    if not isinstance(services, dict):
        return []

    files: list[Path] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue

        build = service.get("build")
        if build:
            if isinstance(build, str):
                context_dir = _resolve_compose_relative(compose_dir, build)
                dockerfile = context_dir / "Dockerfile" if context_dir else None
            elif isinstance(build, dict):
                context = str(build.get("context") or ".")
                context_dir = _resolve_compose_relative(compose_dir, context)
                dockerfile_name = str(build.get("dockerfile") or "Dockerfile")
                dockerfile = (
                    _resolve_build_relative(context_dir, dockerfile_name)
                    if context_dir
                    else None
                )
            else:
                context_dir = None
                dockerfile = None

            if dockerfile:
                files.extend(_iter_path_files(dockerfile))
                if context_dir:
                    files.extend(_dockerfile_copy_sources(dockerfile, context_dir))

        volumes = service.get("volumes", [])
        if isinstance(volumes, list):
            for volume in volumes:
                if isinstance(volume, str):
                    source = volume.split(":", 1)[0]
                elif isinstance(volume, dict):
                    source = str(volume.get("source") or "")
                    if volume.get("type") not in (None, "bind"):
                        continue
                else:
                    continue
                if not source.startswith("."):
                    continue
                resolved = _resolve_compose_relative(compose_dir, source)
                if resolved:
                    files.extend(_iter_path_files(resolved))

    return sorted(set(files))


def _artifact_hash_payload(compose_path: str, compose_content: str) -> str:
    root = Path.cwd().resolve()
    lines: list[str] = []
    for path in _compose_artifact_files(compose_path, compose_content):
        try:
            label = str(path.relative_to(root))
        except ValueError:
            label = str(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{label}:{digest}")
    return "\n".join(lines)


def _parse_env_text(env_text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _preserve_runtime_env(env_str: str, existing_env: str | None) -> str:
    desired = _parse_env_text(env_str)
    existing = _parse_env_text(existing_env or "")
    for key in RUNTIME_ENV_KEYS_TO_PRESERVE:
        if key not in desired and key in existing:
            desired[key] = existing[key]
    return "\n".join(f"{key}={value}" for key, value in desired.items())


class Deployer:
    """Base class for service deployment.

    Subclass and set: service, compose_path, data_path
    Optional: secret_key (name in Vault), env_var_name (display name)
    """

    # Required
    service: str = ""
    compose_path: str = ""
    data_path: str = ""
    project: str = "platform"  # Default project

    # Optional with defaults
    uid: str = "999"
    gid: str = "999"
    chmod: str = "755"  # Override to "700" for sensitive services like PostgreSQL
    secret_key: str = "password"
    env_var_name: str = ""

    # Domain configuration (optional)
    subdomain: str = None  # e.g., "sso" for sso.{INTERNAL_DOMAIN}
    service_port: int = None  # Container port
    service_name: str = None  # For multi-service composes
    deployment_record_timeout_seconds: int = 60
    deployment_record_interval_seconds: int = 3

    @classmethod
    def env(cls) -> dict[str, str | None]:
        return get_env()

    @classmethod
    def project_name(cls, env: dict | None = None) -> str:
        """Get project name, prioritizing class attribute over env var.

        The class `project` attribute takes precedence. Only uses PROJECT env var
        if the class uses the default 'platform' project (for backward compat).
        """
        if cls.project != "platform":
            # Class explicitly sets a non-default project, use it
            return cls.project
        e = env or cls.env()
        return e.get("PROJECT") or cls.project

    @classmethod
    def data_path_for_env(cls, env: dict | None = None) -> str:
        e = env or cls.env()
        explicit_path = e.get("DATA_PATH")
        if explicit_path:
            return explicit_path
        env_name = e.get("ENV", "production")
        project = cls.project_name(e)
        if env_name == "production" or project == "bootstrap":
            return cls.data_path
        suffix = e.get("ENV_SUFFIX")
        if suffix:
            return f"{cls.data_path}{suffix}" if cls.data_path else ""
        if os.environ.get("ALLOW_SHARED_DATA_PATH") == "1":
            return cls.data_path
        raise ValueError(
            "Non-production requires DATA_PATH or ENV_SUFFIX to avoid data collisions. "
            "Set DATA_PATH (recommended) or ENV_SUFFIX; override with ALLOW_SHARED_DATA_PATH=1 if intentional."
        )

    @classmethod
    def compose_env_base(cls, env: dict | None = None) -> dict[str, str]:
        e = env or cls.env()
        base = {
            "ENV": e.get("ENV", "production"),
            "ENV_DOMAIN_SUFFIX": e.get("ENV_DOMAIN_SUFFIX"),
            "INTERNAL_DOMAIN": e.get("INTERNAL_DOMAIN"),
        }
        data_path = cls.data_path_for_env(e)
        if data_path:
            base["DATA_PATH"] = data_path
        if e.get("ENV_SUFFIX"):
            base["ENV_SUFFIX"] = e.get("ENV_SUFFIX")
        return {k: v for k, v in base.items() if v is not None}

    @classmethod
    def secrets(cls):
        """Get secrets backend for this service"""
        e = cls.env()
        # Use cls.project if PROJECT env not set
        project = cls.project_name(e)
        return get_secrets(
            project=project, service=cls.service, env=e.get("ENV", "production")
        )

    @classmethod
    def ensure_runtime_secrets(cls, c: "Context" | None = None) -> bool:
        """Ensure all Vault secrets required by the runtime template exist."""
        secrets_backend = cls.secrets()

        if cls.secret_key:
            try:
                val = secrets_backend.get(cls.secret_key)
            except VaultSecrets.VaultSecretNotFoundError:
                val = None
            if not val:
                val = generate_password(24)
                if secrets_backend.set(cls.secret_key, val):
                    warning(f"Generated new secret in Vault: {cls.secret_key}")
                else:
                    error(f"Failed to store secret in Vault: {cls.secret_key}")
                    return False
            else:
                info(f"Vault secret exists: {cls.secret_key}")
        return True

    @classmethod
    def _prepare_dirs(cls, c: "Context") -> bool:
        """Create data directories on VPS"""
        if missing := validate_env():
            error(f"Missing: {', '.join(missing)}")
            return False

        e = cls.env()
        try:
            data_path = cls.data_path_for_env(e)
        except ValueError as exc:
            error(str(exc))
            return False
        header(f"{cls.service} pre_compose", f"Preparing ({e['ENV']})")

        host = e["VPS_HOST"]
        run_with_status(
            c, f"ssh root@{host} 'mkdir -p {data_path}'", "Create directory"
        )
        run_with_status(
            c,
            f"ssh root@{host} 'chown -R {cls.uid}:{cls.gid} {data_path}'",
            "Set ownership",
        )
        run_with_status(
            c, f"ssh root@{host} 'chmod -R {cls.chmod} {data_path}'", "Set permissions"
        )
        return True

    @classmethod
    def pre_compose(cls, c: "Context") -> dict | None:
        """Prepare directories and ensure secrets exist in Vault.

        For vault-init pattern: secrets are fetched at container runtime,
        so we only ensure they exist and return VAULT_ADDR.
        """
        if not cls._prepare_dirs(c):
            return None

        e = cls.env()

        if not cls.ensure_runtime_secrets(c):
            return None

        # Return base env vars + VAULT_ADDR for vault-init pattern
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        env_vars("DOKPLOY ENV (vault-init)", result)
        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result

    @classmethod
    def get_compose_content(cls, c: "Context") -> str:
        """Get compose file content. Default: read from compose_path."""
        try:
            with open(cls.compose_path, "r") as f:
                return f.read()
        except FileNotFoundError:
            error(f"Compose file not found at path: {cls.compose_path}")
            raise
        except OSError as exc:
            error(f"Failed to read compose file at '{cls.compose_path}': {exc}")
            raise

    @classmethod
    def composing(cls, c: "Context", env_vars: dict[str, str]) -> str:
        """Deploy via Dokploy API using GitHub provider. Returns composeId."""
        from libs.dokploy import get_dokploy, ensure_project
        from libs.const import GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH

        e = cls.env()
        header(f"{cls.service} composing", "Deploying via Dokploy API (GitHub)")
        # Deploy via API
        # Priority: ENV > Class Attribute > Default "platform"
        env_name = e.get("ENV", "production")
        project_name = cls.project_name(e)
        domain = e.get("INTERNAL_DOMAIN")
        host = f"cloud.{domain}" if domain else None

        client = get_dokploy(host=host)

        # Ensure project exists
        project_id, env_id = ensure_project(
            project_name,
            f"Platform services: {project_name}",
            host=host,
            env_name=env_name,
            require_env=env_name != "production",
        )
        if not env_id:
            error("Failed to get environment ID")
            raise ValueError("Failed to get environment ID")

        # Get GitHub provider ID
        github_id = client.get_github_provider_id()
        if not github_id:
            error(
                "No GitHub provider configured in Dokploy. Please add one in Settings -> Git Providers."
            )
            raise ValueError("No GitHub provider found")

        info(f"Using GitHub provider: {github_id}")

        # Format env vars
        env_str = "\n".join(f"{k}={v}" for k, v in env_vars.items() if v is not None)

        # Check if compose already exists
        existing = client.find_compose_by_name(
            cls.service, project_name, env_name=env_name
        )

        if existing:
            compose_id = existing["composeId"]
            info("Updating existing compose service")
            existing_env = client.get_compose(compose_id).get("env")
            client.update_compose(
                compose_id,
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
                env=_preserve_runtime_env(env_str, existing_env),
            )
        else:
            info("Creating new compose service with GitHub provider")
            result = client.create_compose(
                environment_id=env_id,
                name=cls.service,
                app_name=f"{project_name}-{cls.service}",
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
                env=env_str,
            )
            compose_id = result["composeId"]
            # Dokploy initializes new GitHub compose records with default source
            # fields on some versions; update immediately so first deploy uses
            # the intended repository and compose path.
            client.update_compose(
                compose_id,
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
                env=env_str,
            )

        # Deploy
        info(f"Deploying compose {compose_id}...")
        cls._deploy_compose_with_record_check(client, compose_id)

        # Configure domain if specified
        if cls.subdomain and cls.service_port:
            domain_host = service_domain(cls.subdomain, e)
            if not domain_host:
                warning("Domain configuration skipped: INTERNAL_DOMAIN missing")
            else:
                info(f"Ensuring domain: {domain_host}")
                desired_domains = [
                    {"host": domain_host, "port": cls.service_port, "https": True}
                ]
                result = client.ensure_domains(
                    compose_id=compose_id,
                    desired_domains=desired_domains,
                    service_name=cls.service_name,
                )
                if result["created"] > 0:
                    success(f"Domain configured: https://{domain_host}")
                    # Redeploy to apply domain labels
                    info("Redeploying to apply domain labels...")
                    cls._deploy_compose_with_record_check(client, compose_id)
                    success("Domain labels updated")
                elif result["skipped"] > 0:
                    info(f"Domain already configured: {domain_host}")
                if result["conflicts"]:
                    for c in result["conflicts"]:
                        warning(
                            f"Domain conflict: {c['host']} exists with port {c['existing_port']}, need {c['desired_port']}"
                        )

        success(f"Deployed {cls.service} (composeId: {compose_id})")
        return compose_id

    @classmethod
    def _deploy_compose_with_record_check(
        cls,
        client: Any,
        compose_id: str,
        *,
        timeout_seconds: int | None = None,
        interval_seconds: int | None = None,
    ) -> None:
        """Trigger deploy and fail fast if Dokploy does not record runtime work."""
        timeout = int(
            os.getenv(
                "DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS",
                str(
                    timeout_seconds
                    if timeout_seconds is not None
                    else cls.deployment_record_timeout_seconds
                ),
            )
        )
        interval = int(
            os.getenv(
                "DOKPLOY_DEPLOYMENT_RECORD_INTERVAL_SECONDS",
                str(
                    interval_seconds
                    if interval_seconds is not None
                    else cls.deployment_record_interval_seconds
                ),
            )
        )

        before_ids = cls._deployment_ids(
            cls._get_compose_deployments(client, compose_id)
        )
        client.deploy_compose(compose_id)
        if cls._wait_for_new_deployment_record(
            client, compose_id, before_ids, timeout, interval
        ):
            return

        warning(
            "Dokploy deploy did not produce a new deployment record; retrying with compose.redeploy"
        )
        before_ids = cls._deployment_ids(
            cls._get_compose_deployments(client, compose_id)
        )
        client.redeploy_compose(compose_id)
        if cls._wait_for_new_deployment_record(
            client, compose_id, before_ids, timeout, interval
        ):
            return

        raise RuntimeError(
            "Dokploy deploy/redeploy did not produce a new deployment record; "
            "runtime may still be running stale code"
        )

    @staticmethod
    def _deployment_ids(deployments: list[dict]) -> set[str]:
        return {
            str(deployment.get("deploymentId") or deployment.get("id") or "")
            for deployment in deployments
            if deployment.get("deploymentId") or deployment.get("id")
        }

    @staticmethod
    def _get_compose_deployments(client: Any, compose_id: str) -> list[dict]:
        get_compose_deployments = getattr(client, "get_compose_deployments", None)
        if callable(get_compose_deployments):
            try:
                deployments = get_compose_deployments(compose_id)
            except Exception:  # noqa: BLE001 - keep compose snapshot as compatibility fallback.
                deployments = client.get_compose(compose_id).get("deployments")
        else:
            deployments = client.get_compose(compose_id).get("deployments")
        return (
            [deployment for deployment in deployments if isinstance(deployment, dict)]
            if isinstance(deployments, list)
            else []
        )

    @classmethod
    def _wait_for_new_deployment_record(
        cls,
        client: Any,
        compose_id: str,
        previous_ids: set[str],
        timeout_seconds: int,
        interval_seconds: int,
    ) -> bool:
        deadline = time.monotonic() + max(0, timeout_seconds)
        while True:
            deployments = cls._get_compose_deployments(client, compose_id)
            current_ids = cls._deployment_ids(deployments)
            new_ids = current_ids - previous_ids
            if new_ids and isinstance(deployments, list):
                for deployment in deployments:
                    deployment_id = str(
                        deployment.get("deploymentId") or deployment.get("id") or ""
                    )
                    if deployment_id not in new_ids:
                        continue
                    status = str(deployment.get("status") or "").lower()
                    if status == "error":
                        raise RuntimeError("Dokploy deployment record entered error")
                    if status in {"running", "done", "success", "successful"}:
                        return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(1, interval_seconds))

    @classmethod
    def post_compose(cls, c: "Context", shared_tasks: Any) -> bool:
        """Verify deployment"""
        header(f"{cls.service} post_compose", "Verifying")
        result = shared_tasks.status(c)
        if result["is_ready"]:
            success(f"post_compose complete - {result['details']}")
            return True
        error("Verification failed", result["details"])
        return False

    @classmethod
    def get_remote_config_hash(cls) -> str | None:
        """Get config hash stored in Dokploy compose description/env."""
        from libs.dokploy import get_dokploy

        e = cls.env()
        env_name = e.get("ENV", "production")
        project_name = cls.project_name(e)
        domain = e.get("INTERNAL_DOMAIN")
        host = f"cloud.{domain}" if domain else None

        client = get_dokploy(host=host)
        existing = client.find_compose_by_name(
            cls.service, project_name, env_name=env_name
        )

        if not existing:
            return None

        # Hash is stored in env as IAC_CONFIG_HASH
        env_str = existing.get("env", "")
        for line in env_str.split("\n"):
            if line.startswith("IAC_CONFIG_HASH="):
                return line.split("=", 1)[1].strip()
        return None

    @classmethod
    def compute_local_config_hash(cls, c: "Context", env_vars: dict[str, str]) -> str:
        """Compute hash of local compose + env vars."""
        compose_content = cls.get_compose_content(c)
        artifact_payload = _artifact_hash_payload(cls.compose_path, compose_content)
        return _compute_config_hash(compose_content, env_vars, artifact_payload)

    @classmethod
    def verify_vault_app_token(cls) -> dict:
        """Verify VAULT_APP_TOKEN stored in Dokploy is valid."""
        from libs.dokploy import get_dokploy

        e = cls.env()
        env_name = e.get("ENV", "production")
        project_name = cls.project_name(e)
        domain = e.get("INTERNAL_DOMAIN")
        host = f"cloud.{domain}" if domain else None

        client = get_dokploy(host=host)
        existing = client.find_compose_by_name(
            cls.service, project_name, env_name=env_name
        )

        if not existing:
            return {"valid": True, "error": None, "details": "No existing deployment"}

        env_str = existing.get("env", "")
        token = None
        for line in env_str.split("\n"):
            if line.startswith("VAULT_APP_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break

        if not token:
            return {"valid": True, "error": None, "details": "No VAULT_APP_TOKEN found"}

        vault_addr = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        result = verify_vault_token(token, addr=vault_addr, min_ttl_hours=24)
        if result["valid"]:
            result["details"] = f"Token OK (TTL: {result['ttl_hours']}h)"
        else:
            result["details"] = f"Token invalid: {result['error']}"

        return result

    @classmethod
    def sync(cls, c: "Context", force: bool = False) -> dict:
        """Sync IaC state - update only if config changed.

        Returns:
            dict with keys: action (skipped|updated|created|failed), details
        """
        header(f"{cls.service} sync", "Checking for changes")

        # Prepare env vars (without full pre_compose side effects)
        e = cls.env()
        if missing := validate_env():
            return {"action": "failed", "details": f"Missing env: {', '.join(missing)}"}

        # Pre-check: verify VAULT_APP_TOKEN validity
        try:
            token_status = cls.verify_vault_app_token()
            if not token_status["valid"]:
                return {
                    "action": "failed",
                    "details": (
                        f"VAULT_APP_TOKEN issue: {token_status.get('details', 'unknown')}. "
                        "Run `invoke vault.setup-tokens` for this environment before syncing."
                    ),
                }
            elif token_status.get("ttl_hours", 999) < 48:
                return {
                    "action": "failed",
                    "details": (
                        f"VAULT_APP_TOKEN expires in {token_status['ttl_hours']}h. "
                        "Regenerate it with `invoke vault.setup-tokens` before syncing."
                    ),
                }
        except Exception as exc:
            return {
                "action": "failed",
                "details": f"Could not verify VAULT_APP_TOKEN: {exc}",
            }

        if not cls.ensure_runtime_secrets(c):
            return {
                "action": "failed",
                "details": f"Failed to ensure runtime secrets for {cls.service}",
            }

        # Build env vars
        env_vars_dict = cls.compose_env_base(e)
        env_vars_dict["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        # Compute local hash
        local_hash = cls.compute_local_config_hash(c, env_vars_dict)
        remote_hash = cls.get_remote_config_hash()

        info(f"Local config hash: {local_hash}")
        info(f"Remote config hash: {remote_hash or 'not found'}")

        if not force and local_hash == remote_hash:
            success(f"{cls.service}: config unchanged, skipping deploy")
            return {"action": "skipped", "details": "Config hash matches"}

        # Config changed or force - do full deploy
        if remote_hash is None:
            info("No remote config found, creating new deployment")
        elif force:
            warning("Force sync requested")
        else:
            info(f"Config changed ({remote_hash} -> {local_hash}), deploying")

        # Prepare directories
        if not cls._prepare_dirs(c):
            return {"action": "failed", "details": "Failed to prepare directories"}

        # Add hash to env vars
        env_vars_dict["IAC_CONFIG_HASH"] = local_hash

        # Deploy
        try:
            compose_id = cls.composing(c, env_vars_dict)
        except Exception as exc:
            error(f"Deploy failed: {exc}")
            return {"action": "failed", "details": str(exc)}

        # Post-deploy verification: confirm Dokploy's effective compose env now
        # carries the intended IAC_CONFIG_HASH. This fails closed on the stale-env
        # failure mode where a deploy is accepted but the effective config does
        # not advance to the deployed revision.
        try:
            effective_hash = cls.get_remote_config_hash()
        except Exception as exc:  # noqa: BLE001 - verification must not crash the task.
            error(f"Post-deploy verification could not read effective config: {exc}")
            return {
                "action": "failed",
                "details": f"Post-deploy verification failed: {exc}",
            }
        if effective_hash != local_hash:
            error(
                "Post-deploy verification failed: effective IAC_CONFIG_HASH is stale "
                f"(expected {local_hash}, got {effective_hash or 'none'})"
            )
            return {
                "action": "failed",
                "details": (
                    "Effective remote config is stale after deploy "
                    f"(expected {local_hash}, got {effective_hash or 'none'}); "
                    "runtime may still be running prior config"
                ),
            }

        success(f"{cls.service}: deployed with hash {local_hash}")
        return {
            "action": "updated" if remote_hash else "created",
            "details": f"composeId: {compose_id}",
        }


def make_tasks(deployer_cls: type[Deployer], shared_tasks: Any) -> dict:
    """Generate standard invoke tasks for a deployer"""

    @task
    def status(c):
        """Check service status"""
        return shared_tasks.status(c)

    @task
    def pre_compose(c):
        return deployer_cls.pre_compose(c)

    @task
    def composing(c, env_vars=None):
        if env_vars is None:
            warning("Running composing manually - fetching secrets first")
            env_vars = deployer_cls.pre_compose(c)
        if env_vars:
            return deployer_cls.composing(c, env_vars)
        return None

    @task
    def post_compose(c):
        return deployer_cls.post_compose(c, shared_tasks)

    @task
    def setup(c):
        """Full setup - skips if healthy"""
        try:
            result = shared_tasks.status(c)
            if result.get("is_ready"):
                success(f"{deployer_cls.service} already healthy - skipping")
                return
        except Exception as exc:
            warning(f"Status check failed: {exc}")

        warning(f"{deployer_cls.service} not healthy - starting install")
        env_vars = deployer_cls.pre_compose(c)
        if env_vars is None:
            error("pre_compose failed")
            return
        deployer_cls.composing(c, env_vars)
        deployer_cls.post_compose(c, shared_tasks)
        success(f"{deployer_cls.service} setup complete!")

    @task
    def sync(c, force=False):
        """Sync IaC state - deploy only if config changed"""
        return deployer_cls.sync(c, force=force)

    return {
        "status": status,
        "pre_compose": pre_compose,
        "composing": composing,
        "post_compose": post_compose,
        "setup": setup,
        "sync": sync,
    }
