"""Base deployer with DRY task generation

Simplified: minimal class attributes, uses new env.py API.
"""

from __future__ import annotations
from typing import TYPE_CHECKING, Any
from pathlib import Path
import os
import hashlib
import json
import re
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

# Runtime-only AppRole credentials injected into Dokploy env out-of-band (by
# bootstrap/05.vault setup-approle), not present in the git-derived desired env.
# They must survive a redeploy that regenerates the env, otherwise the
# vault-agent loses its credentials and crash-loops (#257/#259/#369). The legacy
# static VAULT_APP_TOKEN was dropped here in #369's v2 cleanup once every service
# was on AppRole — leaving it out also lets a redeploy clean up any vestigial copy.
# Ordered (not a set) so the preserved keys append deterministically — set
# iteration order would vary the generated env line order and churn the config
# hash, causing spurious redeploys.
RUNTIME_ENV_KEYS_TO_PRESERVE = (
    "VAULT_ROLE_ID",
    "VAULT_SECRET_ID",
)

SOURCE_CONFIG_HASH_VERSION = "v1"
EXACT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def discover_services() -> dict[str, str]:
    """Discover deployable services based on deploy.py files."""
    root = Path(__file__).resolve().parents[2]
    service_map: dict[str, str] = {}

    layers = {
        "platform": root / "platform",
        "finance_report": root / "finance_report" / "finance_report",
        "truealpha": root / "truealpha" / "truealpha",
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
            # App layers get prefixed task names (tools/loader.py uses the same
            # prefixes) to avoid colliding with platform/postgres etc.
            task_prefix = {"finance_report": "fr-", "truealpha": "ta-"}.get(layer, "")
            # Invoke exposes collection underscores as dashes on its CLI. The runner
            # executes these values verbatim, so discovery must return the CLI name.
            task_name = service_name.replace("_", "-")
            service_map[key] = f"{task_prefix}{task_name}.sync"

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


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_rel(path: Path) -> str:
    """Repo-relative label for a file, anchored at the REPO ROOT (not ``Path.cwd()``), so the
    config hash is reproducible from any working directory / checkout — the property the
    config-drift reconciler needs to recompute a service's hash at an arbitrary git ref. An
    out-of-tree path keeps its absolute form (never happens for in-repo artifacts/deps)."""
    try:
        return str(path.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def config_hash_from_items(
    compose_content: str,
    env_vars: dict[str, str],
    artifact_items: list[tuple[str, bytes]],
    dep_items: list[tuple[str, bytes]],
) -> str:
    """Pure config hash from explicit inputs — no filesystem / cwd access in here.

    ``artifact_items`` / ``dep_items`` are ``(repo-relative-label, content-bytes)`` pairs
    (artifact in discovery order; deps caller-sorted). Because labels are repo-relative and
    content is passed in, feeding the SAME (compose, env, files) yields the SAME hash whether
    the files were gathered from disk (the deploy path) or from a git ref (the drift
    reconciler) — there is no second, divergent implementation to disagree with the deploy.
    """
    art = [f"{lbl}:{hashlib.sha256(c).hexdigest()}" for lbl, c in artifact_items]
    deps = [f"dep:{lbl}:{hashlib.sha256(c).hexdigest()}" for lbl, c in dep_items]
    payload = "\n".join(art)
    if deps:
        payload = f"{payload}\n" + "\n".join(deps)
    return _compute_config_hash(compose_content, env_vars, payload)


def _artifact_items_from_disk(
    compose_path: str, compose_content: str
) -> list[tuple[str, bytes]]:
    """(repo-relative label, content) for each compose build-context file, on disk."""
    return [
        (_repo_rel(path), path.read_bytes())
        for path in _compose_artifact_files(compose_path, compose_content)
    ]


def _dependency_items_from_disk(compose_path: str) -> list[tuple[str, bytes]]:
    """(repo-relative label, content) for a service's DECLARED extra build/config dependencies,
    sorted by path. Empty unless the service lists `depends_on` globs in deploy-dependencies.yaml
    (a no-op for services that only depend on their own directory)."""
    import glob as _glob
    from libs.deploy_dependencies import (
        extra_dependency_globs,
        service_key_from_path,
    )

    key = service_key_from_path(compose_path)
    if not key:
        return []
    globs = extra_dependency_globs(key)
    if not globs:
        return []

    matched: set[Path] = set()
    for pattern in globs:
        for hit in _glob.glob(str(_REPO_ROOT / pattern), recursive=True):
            p = Path(hit)
            # Exclude transient __pycache__/.pyc/.pyo (mirrors _iter_path_files). The iac-runner
            # deploys from a CLEAN git checkout that has none, so its hash already ignores them;
            # without this exclusion a dev machine (which has compiled .pyc) computes a DIFFERENT
            # hash than the iac-runner for any dep-baking service — non-reproducible by accident.
            if (
                p.is_file()
                and "__pycache__" not in p.parts
                and not p.name.endswith((".pyc", ".pyo"))
            ):
                matched.add(p.resolve())

    return [(_repo_rel(path), path.read_bytes()) for path in sorted(matched)]


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
    # Sub-trees of data_path that must run as a DIFFERENT uid/gid than the service's
    # blanket `chown -R {uid}`. Mapping `relative_subpath -> (uid, gid)`. _prepare_dirs
    # re-asserts these AFTER the blanket chown so a service-managed island (e.g. an
    # embedded ClickHouse running as uid 101 inside a uid-1000 service tree) is never
    # clobbered back. Empty for almost every service.
    data_subpath_uids: dict[str, tuple[str, str]] = {}
    env_var_name: str = ""
    # Set True for services that only exist in production. Observability/analytics
    # (signoz, clickhouse, openpanel) have no prod-correctness blast radius and all
    # environments ship their data to the single prod instance, so a staging copy
    # is pure cost — sync() skips these on non-production envs.
    prod_only: bool = False

    # Domain configuration (optional)
    subdomain: str = None  # e.g., "sso" for sso.{INTERNAL_DOMAIN}
    service_port: int = None  # Container port
    service_name: str = None  # For multi-service composes
    telemetry_service_name: str = None  # OpenTelemetry service.name override
    telemetry_component: str = None  # OpenTelemetry infra.component override
    deployment_record_timeout_seconds: int = 60
    deployment_record_interval_seconds: int = 3
    # Keys supplied by a runtime secret backend must affect deployment
    # idempotence, but cannot be reconstructed from a release in read-only CI.
    runtime_only_config_keys: frozenset[str] = frozenset()

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
    def source_config_env_base(cls, env: dict | None = None) -> dict[str, str]:
        """Return release-recomputable env inputs for source config identity."""
        if cls.runtime_only_config_keys:
            raise NotImplementedError(
                f"{cls.__name__} declares runtime-only config and must implement "
                "source_config_env_base without reading its secret backend"
            )
        result = cls.compose_env_base(env)
        return result

    @classmethod
    def config_env_with_vault_addr(
        cls, env_vars: dict[str, str], env: dict | None = None
    ) -> dict[str, str]:
        """Apply the shared VAULT_ADDR default used by both config identities."""
        e = env or cls.env()
        result = dict(env_vars)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )
        return result

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
        # Re-assert service-managed sub-tree ownership AFTER the blanket chown above, so a
        # sub-island that must run as a different uid (e.g. op-ch ClickHouse = 101 inside
        # the uid-1000 openpanel tree) is not clobbered back to {cls.uid}. _prepare_dirs
        # runs on every sync (right before composing), so this must win every time.
        for subpath, (sub_uid, sub_gid) in cls.data_subpath_uids.items():
            run_with_status(
                c,
                f"ssh root@{host} 'mkdir -p {data_path}/{subpath} "
                f"&& chown -R {sub_uid}:{sub_gid} {data_path}/{subpath}'",
                f"Set ownership ({subpath} -> {sub_uid}:{sub_gid})",
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
        info(
            "\nNote: AppRole creds (VAULT_ROLE_ID/VAULT_SECRET_ID) auto-configured via 'invoke vault.setup-approle'"
        )
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

        # Resolve branch dynamically to support deploying non-main commits/tags
        branch = GITHUB_BRANCH
        try:
            import subprocess

            tag_res = subprocess.run(
                ["git", "describe", "--tags", "--exact-match"],
                capture_output=True,
                text=True,
                check=False,
            )
            if tag_res.returncode == 0 and tag_res.stdout.strip():
                branch = tag_res.stdout.strip()
            else:
                sha_res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if sha_res.returncode == 0 and sha_res.stdout.strip():
                    branch = sha_res.stdout.strip()
        except Exception:
            pass

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

        # autoDeploy=False on every path. These services are deployed by the
        # iac-runner, which already does change detection via the content
        # config-hash gate (compose + env + mounted/build artifacts) — a more
        # precise "minimal restart" than Dokploy's path-based redeploy. Dokploy
        # defaults new composes to autoDeploy=true (with empty watchPaths =>
        # redeploy-on-every-push), which double-triggers with the iac-runner and
        # floods the single-concurrency deploy queue. Keep the iac-runner as the
        # single GitOps trigger; re-assert on update so a manual toggle can't
        # regress it.
        # Resolve the env that will actually be deployed (existing composes
        # preserve runtime creds like VAULT_ROLE_ID from Dokploy), then fail
        # closed if an AppRole compose would ship without its role/secret — the
        # #257/#290 foot-gun where the vault-agent crash-loops on
        # "VAULT_ROLE_ID and VAULT_SECRET_ID are required".
        if existing:
            compose_id = existing["composeId"]
            existing_env = client.get_compose(compose_id).get("env")
            effective_env = _preserve_runtime_env(env_str, existing_env)
        else:
            effective_env = env_str
        cls._assert_approle_creds_present(effective_env)

        if existing:
            info("Updating existing compose service")
            client.update_compose(
                compose_id,
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=branch,
                composePath=cls.compose_path,
                env=effective_env,
                autoDeploy=False,
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
                branch=branch,
                composePath=cls.compose_path,
                env=effective_env,
                autoDeploy=False,
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
                branch=branch,
                composePath=cls.compose_path,
                env=env_str,
                autoDeploy=False,
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
        timeout = cls._resolve_record_timeout(timeout_seconds)
        interval = cls._resolve_record_interval(interval_seconds)

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
    def get_remote_config_identity(cls) -> dict[str, str | None]:
        """Read the config identity stored in Dokploy's effective compose env."""
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
            return {
                "runtime_hash": None,
                "source_hash": None,
                "deploy_ref": None,
                "identity_schema": None,
                "managed_by": None,
                "service_id": None,
                "environment": None,
            }

        env_str = existing.get("env", "")
        values: dict[str, str] = {}
        for line in env_str.split("\n"):
            key, separator, value = line.partition("=")
            if separator and key in {
                "IAC_CONFIG_HASH",
                "IAC_SOURCE_CONFIG_HASH",
                "IAC_DEPLOY_REF",
                "INFRA_IDENTITY_SCHEMA",
                "INFRA_MANAGED_BY",
                "INFRA_SERVICE_ID",
                "INFRA_ENVIRONMENT",
            }:
                values[key] = value.strip()
        return {
            "runtime_hash": values.get("IAC_CONFIG_HASH"),
            "source_hash": values.get("IAC_SOURCE_CONFIG_HASH"),
            "deploy_ref": values.get("IAC_DEPLOY_REF"),
            "identity_schema": values.get("INFRA_IDENTITY_SCHEMA"),
            "managed_by": values.get("INFRA_MANAGED_BY"),
            "service_id": values.get("INFRA_SERVICE_ID"),
            "environment": values.get("INFRA_ENVIRONMENT"),
        }

    @classmethod
    def get_remote_config_hash(cls) -> str | None:
        """Backward-compatible accessor for the runtime idempotence hash."""
        return cls.get_remote_config_identity()["runtime_hash"]

    @classmethod
    def _resolve_record_timeout(cls, timeout_seconds: int | None = None) -> int:
        """Deployment-record / hash-poll timeout, honoring the env override.

        Shared by the deploy-record wait and the post-deploy hash poll so an operator
        who raises DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS to tolerate a slow Dokploy
        widens both windows, not just one.
        """
        return int(
            os.getenv(
                "DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS",
                str(
                    timeout_seconds
                    if timeout_seconds is not None
                    else cls.deployment_record_timeout_seconds
                ),
            )
        )

    @classmethod
    def _resolve_record_interval(cls, interval_seconds: int | None = None) -> int:
        """Deployment-record / hash-poll interval, honoring the env override."""
        return int(
            os.getenv(
                "DOKPLOY_DEPLOYMENT_RECORD_INTERVAL_SECONDS",
                str(
                    interval_seconds
                    if interval_seconds is not None
                    else cls.deployment_record_interval_seconds
                ),
            )
        )

    @classmethod
    def _await_effective_config_hash(cls, expected_hash: str) -> str | None:
        """Poll Dokploy's effective IAC_CONFIG_HASH until it matches `expected_hash`
        or the deployment timeout elapses, returning the last value read.

        Dokploy applies the compose-env update asynchronously, so a read taken
        immediately after deploy can lag the deployed revision (returning a stale
        hash or none). Polling avoids a false "stale config" verdict on that
        settling delay while still surfacing a genuinely-unadvanced config (the
        returned hash will still differ from `expected_hash` once the window
        elapses).

        A transient read error (one flaky Dokploy `compose.one`) does NOT abort the
        deploy: polling multiplies the number of reads, so each is an independent
        chance to hit a blip. A read error is tolerated like a non-matching read and
        retried until the deadline; only if no clean read ever lands in the whole
        window is the last error surfaced. The timeout/interval honor the same env
        overrides as the deploy-record wait.
        """
        deadline = time.monotonic() + cls._resolve_record_timeout()
        interval = max(1, cls._resolve_record_interval())
        last_value: str | None = None
        last_error: Exception | None = None
        while True:
            try:
                last_value = cls.get_remote_config_hash()
                last_error = None
            except Exception as exc:  # transient Dokploy read; tolerate within window
                last_error = exc
            if last_value == expected_hash:
                return last_value
            if time.monotonic() >= deadline:
                if last_value is None and last_error is not None:
                    raise last_error
                return last_value
            time.sleep(interval)

    @classmethod
    def _assert_approle_creds_present(cls, effective_env: str) -> None:
        """Fail closed if this service's compose uses Vault AppRole auth but the
        env about to be deployed lacks role/secret creds.

        Prevents the #257/#290 foot-gun: an AppRole config change (token_file ->
        approle) lands without VAULT_ROLE_ID/VAULT_SECRET_ID, so the vault-agent
        crash-loops on "VAULT_ROLE_ID and VAULT_SECRET_ID are required" and the
        service never starts. Run `vault.setup-approle` first.
        """
        from pathlib import Path

        # Read the compose WITHOUT swallowing errors: it is a required,
        # version-controlled artifact (already read for the config hash), so an
        # unreadable compose is a real problem — failing closed beats skipping the
        # preflight and re-opening the foot-gun.
        compose_text = Path(cls.compose_path).read_text(encoding="utf-8")
        if (
            "VAULT_ROLE_ID" not in compose_text
            and "VAULT_SECRET_ID" not in compose_text
        ):
            return  # service does not use AppRole auth

        env = _parse_env_text(effective_env)
        missing = [
            key
            for key in ("VAULT_ROLE_ID", "VAULT_SECRET_ID")
            if not (env.get(key) or "").strip()
        ]
        if missing:
            e = cls.env()
            raise ValueError(
                f"{cls.service}: compose uses Vault AppRole auth but "
                f"{', '.join(missing)} is missing from the deploy env — the vault-agent "
                "would crash-loop on 'VAULT_ROLE_ID and VAULT_SECRET_ID are required'. "
                f"Run `DEPLOY_ENV={e.get('ENV', 'production')} invoke vault.setup-approle "
                f"--project {cls.project_name(e)} --service {cls.service} --deploy` before "
                "deploying."
            )

        # Role/secret are present — also require VAULT_ADDR. The compose declares
        # `VAULT_ADDR: ${VAULT_ADDR}` with NO default, and the vault-agent entrypoint only
        # guards ROLE_ID/SECRET_ID, so a missing VAULT_ADDR slips through to runtime where
        # the agent hangs reaching an empty address and the service deadlocks on its
        # healthcheck (~6 min) with no clear cause. Fail fast here, symmetric with the
        # creds — the preview path already guards VAULT_ADDR (preview_lifecycle
        # ``_VAULT_CRED_KEYS``); this closes the same gap on the fixed staging/prod path.
        if not (env.get("VAULT_ADDR") or "").strip():
            raise ValueError(
                f"{cls.service}: compose uses Vault AppRole auth but VAULT_ADDR is missing "
                "from the deploy env — the vault-agent would hang reaching an empty Vault "
                "address and the service would deadlock on its healthcheck. Set VAULT_ADDR "
                "(e.g. https://vault.<INTERNAL_DOMAIN>) on the compose/project env before "
                "deploying."
            )

    @classmethod
    def compute_local_config_hash(cls, c: "Context", env_vars: dict[str, str]) -> str:
        """Compute hash of local compose + env vars + declared shared deps.

        Thin adapter: gather the compose/build-context/dependency files from disk (repo-relative
        labels) and delegate to the path-independent :func:`config_hash_from_items`. Declared
        cross-service dependencies are folded in so a change to a shared artifact this service
        bakes in (a contract, pinned config) flips its hash — keeping the iac-runner fan-out and
        the hash gate in agreement.
        """
        compose_content = cls.get_compose_content(c)
        return config_hash_from_items(
            compose_content,
            env_vars,
            _artifact_items_from_disk(cls.compose_path, compose_content),
            _dependency_items_from_disk(cls.compose_path),
        )

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
        # AppRole services authenticate via VAULT_ROLE_ID/VAULT_SECRET_ID. A vestigial
        # VAULT_APP_TOKEN left in Dokploy is unused and would expire un-renewed, so gating
        # on it would hard-block an AppRole deploy. Skip the legacy token check for them.
        if "VAULT_ROLE_ID=" in env_str and "VAULT_SECRET_ID=" in env_str:
            return {
                "valid": True,
                "error": None,
                "details": "AppRole auth; legacy VAULT_APP_TOKEN preflight skipped",
            }
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
        if cls.prod_only and e.get("ENV", "production") != "production":
            info(f"{cls.service} is prod-only; skipping {e.get('ENV')} sync")
            return {
                "action": "skipped",
                "details": f"prod-only service; not deployed to {e.get('ENV')}",
            }
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
                        "This is a legacy static token; remove it from the service's Dokploy "
                        "env (services authenticate via AppRole now — `invoke vault.setup-approle`)."
                    ),
                }
            elif token_status.get("ttl_hours", 999) < 48:
                return {
                    "action": "failed",
                    "details": (
                        f"VAULT_APP_TOKEN expires in {token_status['ttl_hours']}h. "
                        "It is a legacy static token; remove it from the Dokploy env "
                        "(AppRole services don't use it)."
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
        env_vars_dict = cls.config_env_with_vault_addr(cls.compose_env_base(e), e)
        source_env_vars = cls.config_env_with_vault_addr(
            cls.source_config_env_base(e), e
        )

        # Runtime identity drives deploy idempotence; source identity is secret-free
        # and can be independently reconstructed from the immutable release.
        local_hash = cls.compute_local_config_hash(c, env_vars_dict)
        source_hash = (
            f"{SOURCE_CONFIG_HASH_VERSION}:"
            f"{cls.compute_local_config_hash(c, source_env_vars)}"
        )
        deploy_ref = (os.getenv("IAC_DEPLOY_REF") or "").strip().lower()
        if not deploy_ref:
            try:
                import subprocess

                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                deploy_ref = (
                    result.stdout.strip().lower() if result.returncode == 0 else ""
                )
            except (OSError, subprocess.SubprocessError):
                deploy_ref = ""
        if not EXACT_COMMIT_RE.fullmatch(deploy_ref):
            return {
                "action": "failed",
                "details": "Deployment identity requires an exact 40-character IAC_DEPLOY_REF",
            }
        from libs.deploy_dependencies import service_key_from_path
        from libs.service_identity import ServiceIdentity

        service_id = service_key_from_path(cls.compose_path)
        if not service_id:
            return {
                "action": "failed",
                "details": f"Could not derive service identity from {cls.compose_path}",
            }
        runtime_identity = ServiceIdentity.build(
            service_id,
            e.get("ENV", "production"),
            component=cls.service,
            service_name=cls.telemetry_service_name or cls.service,
            version=deploy_ref,
            iac_ref=deploy_ref,
        )

        # Read the remote (deployed) hash. FAIL CLOSED: reading it hits the
        # Dokploy API, which can error/time out exactly when the host is under
        # load. Treating that failure as "no remote config -> redeploy" forms a
        # positive feedback loop (more jam -> more API errors -> more forced
        # redeploys -> more jam). When the remote state is unreadable, skip and
        # alert instead of deploying. (`--force` still proceeds deliberately.)
        try:
            remote_identity = cls.get_remote_config_identity()
            remote_hash = remote_identity["runtime_hash"]
        except Exception as exc:  # noqa: BLE001 - any lookup failure must fail closed
            if not force:
                warning(
                    f"{cls.service}: remote config hash unreadable ({exc}); "
                    "skipping deploy (fail-closed) to avoid load-amplifying redeploys"
                )
                return {
                    "action": "skipped",
                    "details": f"Remote config unreadable; fail-closed: {exc}",
                }
            warning(
                f"{cls.service}: remote config unreadable ({exc}); "
                "proceeding because --force was requested"
            )
            remote_identity = {
                "runtime_hash": None,
                "source_hash": None,
                "deploy_ref": None,
                "identity_schema": None,
                "managed_by": None,
                "service_id": None,
                "environment": None,
            }
            remote_hash = None

        info(f"Local config hash: {local_hash}")
        info(f"Remote config hash: {remote_hash or 'not found'}")

        remote_ref = remote_identity["deploy_ref"] or ""
        expected_deploy_identity = runtime_identity.deploy_env()
        remote_source_identity_valid = remote_identity[
            "source_hash"
        ] == source_hash and bool(EXACT_COMMIT_RE.fullmatch(remote_ref))
        remote_service_identity_valid = all(
            remote_identity.get(remote_key) == expected_deploy_identity[env_key]
            for remote_key, env_key in (
                ("identity_schema", "INFRA_IDENTITY_SCHEMA"),
                ("managed_by", "INFRA_MANAGED_BY"),
                ("service_id", "INFRA_SERVICE_ID"),
                ("environment", "INFRA_ENVIRONMENT"),
            )
        )
        if (
            not force
            and local_hash == remote_hash
            and remote_source_identity_valid
            and remote_service_identity_valid
        ):
            success(f"{cls.service}: config unchanged, skipping deploy")
            return {
                "action": "skipped",
                "details": "Runtime and source config identities match",
            }

        # Config changed or force - do full deploy
        if remote_hash is None:
            info("No remote config found, creating new deployment")
        elif force:
            warning("Force sync requested")
        elif local_hash == remote_hash:
            info(
                "Runtime config unchanged but release identity is missing or stale; reconciling"
            )
        else:
            info(f"Config changed ({remote_hash} -> {local_hash}), deploying")

        # Prepare directories
        if not cls._prepare_dirs(c):
            return {"action": "failed", "details": "Failed to prepare directories"}

        # Persist both config planes and the exact checked-out revision. These are
        # deliberately excluded from their own hash inputs above.
        env_vars_dict["IAC_CONFIG_HASH"] = local_hash
        env_vars_dict["IAC_SOURCE_CONFIG_HASH"] = source_hash
        env_vars_dict["IAC_DEPLOY_REF"] = deploy_ref
        env_vars_dict.update(runtime_identity.deploy_env())
        if cls.telemetry_service_name:
            telemetry_identity = ServiceIdentity.build(
                service_id,
                e.get("ENV", "production"),
                component=cls.telemetry_component or cls.service,
                service_name=cls.telemetry_service_name,
                version=deploy_ref,
                iac_ref=deploy_ref,
            )
            env_vars_dict["OTEL_SERVICE_NAME"] = telemetry_identity.service_name
            env_vars_dict["OTEL_RESOURCE_ATTRIBUTES"] = (
                telemetry_identity.otel_resource_attributes()
            )

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
        #
        # Dokploy applies the compose-env update asynchronously, so the effective
        # hash can briefly lag the deploy call by a few seconds. Poll until it
        # advances rather than false-failing on that settling delay; still fails
        # closed if it never advances within the window.
        try:
            effective_hash = cls._await_effective_config_hash(local_hash)
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

        try:
            effective_identity = cls.get_remote_config_identity()
        except Exception as exc:  # noqa: BLE001 - identity proof is fail-closed.
            error(f"Post-deploy identity verification failed: {exc}")
            return {
                "action": "failed",
                "details": f"Post-deploy identity verification failed: {exc}",
            }
        identity_mismatches = []
        if effective_identity["source_hash"] != source_hash:
            identity_mismatches.append(
                "IAC_SOURCE_CONFIG_HASH "
                f"expected {source_hash}, got {effective_identity['source_hash'] or 'none'}"
            )
        if deploy_ref and effective_identity["deploy_ref"] != deploy_ref:
            identity_mismatches.append(
                "IAC_DEPLOY_REF "
                f"expected {deploy_ref}, got {effective_identity['deploy_ref'] or 'none'}"
            )
        for remote_key, env_key in (
            ("identity_schema", "INFRA_IDENTITY_SCHEMA"),
            ("managed_by", "INFRA_MANAGED_BY"),
            ("service_id", "INFRA_SERVICE_ID"),
            ("environment", "INFRA_ENVIRONMENT"),
        ):
            expected_value = expected_deploy_identity[env_key]
            if effective_identity.get(remote_key) != expected_value:
                identity_mismatches.append(
                    f"{env_key} expected {expected_value}, "
                    f"got {effective_identity.get(remote_key) or 'none'}"
                )
        if identity_mismatches:
            details = "; ".join(identity_mismatches)
            error(f"Post-deploy identity verification failed: {details}")
            return {
                "action": "failed",
                "details": f"Effective remote identity is stale after deploy: {details}",
            }

        # Runtime-applied verification (closed loop): the hash check above only
        # proves Dokploy RECORDED the intended config. Let services additionally
        # assert that the actually-running container reflects it, catching the
        # failure mode where a deploy is accepted but the container was never
        # recreated (so it keeps running prior config while the catalog says
        # "Live"). Default is a no-op; only services that override it pay the cost.
        runtime_error = cls.verify_runtime_applied(c, env_vars_dict)
        if runtime_error:
            error(f"{cls.service}: runtime verification failed: {runtime_error}")
            return {
                "action": "failed",
                "details": f"Runtime verification failed: {runtime_error}",
            }

        success(f"{cls.service}: deployed with hash {local_hash}")
        return {
            "action": "updated" if remote_hash else "created",
            "details": f"composeId: {compose_id}",
        }

    @classmethod
    def verify_runtime_applied(
        cls, c: "Context", env_vars: dict[str, str]
    ) -> str | None:
        """Optional per-service check that the RUNNING container reflects the
        just-deployed config (not merely that Dokploy recorded the intended
        hash). Return an error string to fail the deploy, or None to pass.

        Default: no-op. Override in services where "recorded" can silently differ
        from "running" (e.g. env-literal changes Dokploy may not recreate on)."""
        return None


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
        """Sync IaC state - deploy only if config changed.

        A FAILED action exits non-zero so the caller (the iac-runner) sees a real
        failure instead of a green "✅ sync completed". A 'skipped' action — incl.
        the deliberate fail-closed skip when the remote hash is unreadable — stays
        a success (exit 0): that safety net is preserved unchanged.
        """
        result = deployer_cls.sync(c, force=force)
        if isinstance(result, dict) and result.get("action") == "failed":
            from invoke.exceptions import Exit

            raise Exit(
                f"{deployer_cls.service} sync failed: "
                f"{result.get('details', 'unknown')}",
                code=1,
            )
        return result

    return {
        "status": status,
        "pre_compose": pre_compose,
        "composing": composing,
        "post_compose": post_compose,
        "setup": setup,
        "sync": sync,
    }
