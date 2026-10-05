"""Base deployer with DRY task generation

Simplified: minimal class attributes, uses new env.py API.
"""

from __future__ import annotations

import os
import shlex
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from invoke import task

from libs.common import get_env, service_domain, validate_env
from libs.console import (
    env_vars,
    error,
    header,
    info,
    run_with_status,
    success,
    warning,
)
from libs.deploy.config_hash import (
    _artifact_items_from_disk,
    _compose_artifact_files,
    _compute_config_hash,
    _dependency_items_from_globs,
    _dockerfile_copy_sources,
    _iter_path_files,
    _repo_rel,
    _resolve_build_relative,
    _resolve_compose_relative,
    config_hash_from_items,
)
from libs.deploy.discovery import (
    discover_services as _discover_services_pure,
    load_deployer_class as _load_deployer_class_pure,
)
from libs.deploy.env_util import (
    RUNTIME_ENV_KEYS_TO_PRESERVE,
    _parse_env_text,
    _preserve_runtime_env,
)
from libs.deploy.sync_pipeline import (
    SOURCE_CONFIG_HASH_VERSION,
    ConsoleLogger,
    SyncAction,
    SyncPipeline,
    SyncResult,
)
from libs.env import get_secrets, verify_vault_token
from libs.service_facets import (
    BackupFacet,
    Exemption,
    ProbeFacet,
    PublicRouteFacet,
    RestartAfterFacet,
    SignalFacet,
)

if TYPE_CHECKING:
    from invoke import Context
    from libs.dokploy import DokployClient


__all__ = [
    "Deployer",
    "RUNTIME_ENV_KEYS_TO_PRESERVE",
    "SOURCE_CONFIG_HASH_VERSION",
    "SyncAction",
    "SyncResult",
    "_artifact_items_from_disk",
    "_compose_artifact_files",
    "_compute_config_hash",
    "_dependency_items_from_disk",
    "_dockerfile_copy_sources",
    "_iter_path_files",
    "_parse_env_text",
    "_preserve_runtime_env",
    "_repo_rel",
    "_resolve_build_relative",
    "_resolve_compose_relative",
    "config_hash_from_items",
    "discover_services",
    "load_deployer_class",
    "make_tasks",
]


# infra2#525: like libs.deploy.promote.wait_for_rollout, Dokploy deployment records
# carry no caller-supplied correlation id, so a start-timestamp floor (captured just
# before OUR OWN deploy/redeploy_compose() call) is the only available signal that a
# "new" record belongs to a DIFFERENT, unrelated trigger rather than to us. Small
# margin for clock skew between this process and Dokploy's server.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5


class _HostStack(NamedTuple):
    """A Dokploy compose project on its host, and a way to ask the host about it."""

    host: str
    project: str
    run: Callable[[str], Any]


class _StackUnobservable(RuntimeError):
    """The host could not be asked about the stack — no evidence either way."""


def discover_services() -> dict[str, str]:
    from libs.service_registry import _LAYERS as layers

    return _discover_services_pure(layers)


def load_deployer_class(service_id: str) -> type[Deployer] | None:
    from libs.service_registry import _LAYERS as layers

    return _load_deployer_class_pure(service_id, layers=layers, base_class=Deployer)


def _dependency_items_from_disk(compose_path: str) -> list[tuple[str, bytes]]:
    from libs.deploy_dependencies import extra_dependency_globs, service_key_from_path

    key = service_key_from_path(compose_path)
    globs = extra_dependency_globs(key) if key else ()
    return _dependency_items_from_globs(globs)


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
    route_preference: Any = None  # Optional infra2_sdk.routing.AppRoutePreference
    # Override the shared INTERNAL_DOMAIN entirely for this service (e.g. a dedicated
    # product domain instead of the platform's shared one). None = use whatever domain
    # the deploy request/caller passes in (today's behavior for every existing service).
    # Read by libs.service_registry / libs.app_deploy_request, not by this class itself —
    # a service with its own compose-level Traefik Host() rules (like subdomain=None
    # above) still needs its INTERNAL_DOMAIN substitution to resolve to the right zone.
    domain: str = None
    service_port: int = None  # Container port
    service_name: str = None  # For multi-service composes
    telemetry_service_name: str = None  # OpenTelemetry service.name override
    telemetry_component: str = None  # OpenTelemetry infra.component override
    # Measured on the VPS (2026-09-07/08): Dokploy takes 45–200 s after `compose.deploy`
    # just to CREATE the deployment record (it clones infra2 from GitHub first), and a few
    # more minutes to reach `done`. 60 s produced a red release lane while the deploy went
    # on — and the redeploy fallback queued a second deployment (#629). 420 s + the 90 s
    # runtime verification stays inside the runner's DEPLOY_TIMEOUT.
    deployment_record_timeout_seconds: int = 420
    deployment_record_interval_seconds: int = 3
    # Keys supplied by a runtime secret backend must affect deployment
    # idempotence, but cannot be reconstructed from a release in read-only CI.
    runtime_only_config_keys: frozenset[str] = frozenset()

    # --- Service facets (#541 convergence): the Deployer subclass is the SINGLE
    # declaration point for per-service operational facts; libs.service_registry
    # .service_attrs() is the single derivation function. Declarations must be
    # LITERAL constructor calls (they are read via AST, never imported) — see
    # libs/service_facets.py for the constraint and field docs.
    probes: tuple[ProbeFacet, ...] = ()
    public_routes: tuple[PublicRouteFacet, ...] = ()
    signals: tuple[SignalFacet, ...] = ()
    backups: tuple[BackupFacet, ...] = ()
    exemptions: tuple[Exemption, ...] = ()
    # Containers of THIS service to restart after another service is redeployed (#726);
    # the dependency's sync performs the restart (restart_dependents).
    restart_after: tuple[RestartAfterFacet, ...] = ()
    # True for services whose deploy path is exercised by the deploy_v2
    # acceptance canary (tools/deploy_v2_canary.py iterates the registry for
    # this flag instead of hardcoding a service id).
    deploy_v2_canary: bool = False

    @classmethod
    def env(cls) -> dict[str, str | None]:
        return get_env()

    @classmethod
    def project_name(cls, env: dict | str | None = None) -> str:
        """Get project name, prioritizing class attribute over env var.

        The class `project` attribute takes precedence. Only uses PROJECT env var
        if the class uses the default 'platform' project (for backward compat).
        """
        if cls.project != "platform":
            # Class explicitly sets a non-default project, use it
            return cls.project
        e = env if isinstance(env, dict) else cls.env()
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
    def compose_env_overrides(
        cls, *, env: str, domain: str, env_suffix: str
    ) -> dict[str, str]:
        """Extra compose env vars a fixed-app deploy (libs.deploy.promote.deploy) should
        merge in beyond its own shared, hardcoded assembly (IMAGE_TAG, INTERNAL_DOMAIN,
        identity.deploy_env(), openpanel_env(), otel_env(), ...). Default: none.

        Deliberately separate from compose_env_base (the iac_pinned/iac-runner sync
        path's hook, which promote.deploy never calls): a fixed-app deploy already has
        `env`/`domain`/`env_suffix` resolved as this call's own explicit parameters, and
        compose_env_base's data_path_for_env validation is irrelevant here. Subclasses
        override this for one-off app-specific Traefik/routing vars promote.py's shared
        assembly has no way to know about (e.g. AppDeployer.APP_HOST, truealpha#474).
        """
        return {}

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
    def secrets_backend(cls, env: str | None = None):
        """Get VaultSecrets instance for this service.

        Renamed from ``secrets`` (#543 hotfix): service Deployers declare a
        ``secrets = (SecretsFacet(...),)`` facet attribute (#542), which
        SHADOWED a same-named classmethod at runtime — every sync that hit
        ``ensure_runtime_secrets`` died with "'tuple' object is not callable"
        (v1.1.35 staging reconcile). Facet attribute names must never collide
        with Deployer callables — enforced by
        libs/tests/test_service_facets.py.

        ``env`` selects the target environment's Vault store. The deploy path
        that already knows which env it is deploying — e.g.
        libs.deploy.promote.deploy(), which handles staging THEN prod within
        one process/CLI invocation — must not depend on os.environ["ENV"]
        happening to match, since nothing sets it for that in-process path
        (unlike the legacy invoke <service>.sync subprocess, which always runs
        with ENV pre-set for its whole lifetime). None keeps today's behavior.
        """
        e = cls.env()
        # Use cls.project if PROJECT env not set
        project = cls.project_name(e)
        service = getattr(cls, "vault_service_name", None) or cls.service
        return get_secrets(
            project=project, service=service, env=env or e.get("ENV", "production")
        )

    @classmethod
    def ensure_runtime_secrets(
        cls, c: "Context" | None = None, *, env: str | None = None
    ) -> bool:
        """Gate runtime-secret readiness before compose deploy or sync.

        Returns True when this service's Vault state is acceptable:
        - Manifest-registered services: True immediately (``apply_secret_supply``
          is the single writer; this method defers to it).
        - Services without a ``secret_key``: True immediately (no Vault
          interaction needed).
        - Unmanifested services with a ``secret_key``: generate-or-verify the
          key in Vault (legacy self-heal path for not-yet-migrated services).

        ``env`` — see secrets_backend(); threaded through so this stays correct
        when called for an env other than the process's own ``ENV``.
        """
        from libs import secrets_registry

        e = cls.env()
        effective_env = env or e.get("ENV", "production")
        project = cls.project_name(e)

        if secrets_registry.lookup(project, cls.service) is not None:
            # The manifest owns this service's store and apply_secret_supply generates
            # what it declares (PR-E: one writer).
            return True

        # Services without a registered manifest and without an explicit secret_key
        # (e.g. services with empty passwords, or pure proxy/label configs) require
        # no Vault runtime secrets.
        if not cls.secret_key:
            return True

        from libs.env import VaultSecrets, generate_password

        secrets_backend = cls.secrets_backend(env=effective_env)

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

    # ---- plan PR-E: the manifest-driven secret supply, applied on every deploy --------
    @classmethod
    def apply_secret_supply(cls, c: "Context", *, env: str | None = None) -> bool:
        """Copy human values from 1Password, generate missing runtime values, mirror the
        ones humans need, and refuse to deploy while the store lacks a required value.

        Services without a registered manifest (libs.secrets_registry) are untouched.
        When a value changed, the vault-agent and the app containers are restarted so the
        rendered file and the processes that sourced it agree (RC4, #640).
        """
        from libs import secrets_registry
        from libs.security import supply as secrets_supply

        e = cls.env()
        env_name = env or e.get("ENV", "production")
        service = secrets_registry.lookup(cls.project_name(e), cls.service)
        if service is None:
            return True

        def restart(changed: tuple[str, ...]) -> None:
            names = cls._secret_consumer_containers(e)
            if not names:
                return
            on_host = cls._on_host(c, e)
            if on_host is None:
                raise RuntimeError(
                    f"VPS_HOST unset; restart by hand: {shlex.join(['docker', 'restart', *names])}"
                )
            # A restart is for a consumer that holds a stale value. On the first deploy of a
            # service its vault-agent and app containers do not exist yet, and `docker
            # restart` of a missing name exits 1: the sync used to fail before it could
            # create them. `ps -a` lists every container that exists, stopped ones too,
            # because `docker restart` starts a stopped consumer with the new value.
            listing = on_host(
                shlex.join(["docker", "ps", "-a", "--format", "{{.Names}}"])
            )
            if not listing.ok:
                # Fail closed: when the host cannot say what exists, consumers could keep
                # a stale value while the store already changed.
                raise RuntimeError(
                    f"could not list containers "
                    f"({(listing.stderr or '').strip() or 'docker ps failed'}); "
                    f"restart by hand: {shlex.join(['docker', 'restart', *names])}"
                )
            existing = {line.strip() for line in listing.stdout.splitlines()}
            present = [name for name in names if name in existing]
            absent = [name for name in names if name not in existing]
            if absent:
                info(
                    f"{cls.service}: not created yet, not restarted: {', '.join(absent)}"
                )
            if not present:
                return
            info(
                f"{cls.service}: {len(changed)} secret value(s) changed; restarting {', '.join(present)}"
            )
            result = on_host(shlex.join(["docker", "restart", *present]))
            if not result.ok:
                # Fail closed (review on #648): consumers would keep running on the
                # previously rendered values while the store already changed.
                raise RuntimeError(
                    f"could not restart {', '.join(present)} after {', '.join(changed)} changed "
                    f"({(result.stderr or '').strip() or 'no output'})"
                )
            success(f"{cls.service}: restarted secret consumers: {', '.join(present)}")

        try:
            report = secrets_supply.apply(service, env_name, restart=restart)
        except Exception as exc:  # noqa: BLE001 - a supply failure must stop the deploy, not crash it
            error(f"{cls.service}: secret supply failed: {exc}")
            return False
        for note in report.notes:
            warning(f"{cls.service}: {note}")
        if not report.ok:
            error(
                f"{cls.service}: Vault still lacks required values: {', '.join(report.missing)}"
            )
            return False
        success(f"{cls.service}: secret supply ok ({report.summary()})")
        return True

    @classmethod
    def _secret_consumer_containers(cls, e: dict) -> list[str]:
        names: list[str] = []
        for facet in getattr(cls, "secrets", ()) or ():
            for name in (facet.vault_agent_container, *facet.app_containers):
                names.append(name.replace("${ENV_SUFFIX}", e.get("ENV_SUFFIX", "")))
        return names

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
        if not data_path:
            return True
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
        if not cls.apply_secret_supply(c, env=e.get("ENV")):
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
        branch = cls._checkout_ref() or GITHUB_BRANCH

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
        if not existing:
            for legacy_name in getattr(cls, "legacy_compose_names", ()):
                existing = client.find_compose_by_name(
                    legacy_name, project_name, env_name=env_name
                )
                if existing:
                    info(f"Adopting legacy compose {legacy_name} -> {cls.service}")
                    client.update_compose(
                        existing["composeId"],
                        name=cls.service,
                        composePath=cls.compose_path,
                    )
                    break

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
        # preserve runtime creds like VAULT_ROLE_ID from Dokploy). The AppRole
        # fail-closed check moved BELOW the create/update block: creating the
        # compose RECORD triggers no deployment (autoDeploy=False everywhere),
        # and asserting before creation deadlocked every first-time
        # environment — sync refused to create the compose without creds while
        # vault.setup-approle had no compose to inject creds into (hit live on
        # truealpha/data_engine's production graduation, 2026-07-27). Order is
        # now: upsert record -> assert creds -> deploy.
        if existing:
            compose_id = existing["composeId"]
            existing_env = client.get_compose(compose_id).get("env")
            effective_env = _preserve_runtime_env(env_str, existing_env)
        else:
            effective_env = env_str

        from libs.deploy.dokploy_adapter import upsert_github_compose

        compose_id = upsert_github_compose(
            client,
            service_name=cls.service,
            project_name=project_name,
            env_id=env_id,
            github_id=github_id,
            repository=GITHUB_REPO,
            owner=GITHUB_OWNER,
            branch=branch,
            compose_path=cls.compose_path,
            effective_env=effective_env,
            existing=existing,
            raw_env_str=env_str,
            log_info=info,
        )

        # Deploy
        # Fail closed BEFORE any deployment if an AppRole compose would ship
        # without its role/secret — the #257/#290 foot-gun where the
        # vault-agent crash-loops on "VAULT_ROLE_ID and VAULT_SECRET_ID are
        # required". The compose record now exists either way, so
        # `vault.setup-approle` has a target to inject into and its
        cls._assert_approle_creds_present(effective_env)

        # Prune stale Dokploy-attached domains before deployment.
        # If cls.subdomain is None (the service manages its own routing via compose.yaml Traefik labels, Infra-011.5)
        # or if the compose was adopted from a legacy name (e.g. minio -> s3), Dokploy DB may have domains
        # attached with a serviceName that no longer exists in compose.yaml. Dokploy validates domain attachments
        # during compose deployment and immediately aborts if any domain references a non-existent service.
        if cls.subdomain is None:
            cls._prune_stale_dokploy_domains(client, compose_id)

        # Configure domains BEFORE deploying compose so Dokploy includes domain
        # labels in a single pass (avoiding an expensive second redeploy).
        cls.ensure_compose_domains(client, compose_id, e)

        info(f"Deploying compose {compose_id}...")
        cls._deploy_compose_with_record_check(client, compose_id)

        success(f"Deployed {cls.service} (composeId: {compose_id})")
        return compose_id

    @classmethod
    def ensure_compose_domains(
        cls, client: DokployClient, compose_id: str, e: dict[str, str]
    ) -> dict:
        """Ensure Dokploy-managed domains are configured BEFORE compose deployment."""
        from libs.deploy.dokploy_adapter import (
            ensure_compose_domains as _ensure_domains,
        )

        return _ensure_domains(
            client,
            compose_id,
            env=e,
            route_pref=getattr(cls, "route_preference", None),
            subdomain=cls.subdomain,
            service_port=cls.service_port,
            service_name=cls.service_name,
            service_domain_fn=service_domain,
            log_info=info,
            log_warning=warning,
            log_success=success,
        )

    @classmethod
    def _checkout_ref(cls) -> str | None:
        """The ref Dokploy is told to check out: the exact tag on HEAD, else HEAD's sha."""
        import subprocess

        for argv in (
            ["git", "describe", "--tags", "--exact-match"],
            ["git", "rev-parse", "HEAD"],
        ):
            try:
                result = subprocess.run(
                    argv, capture_output=True, text=True, check=False
                )
            except OSError:
                return None
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        return None

    @classmethod
    def _checkout_sha(cls, ref: str) -> str | None:
        """What `ref` resolves to here; None when this checkout cannot resolve it."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    # A deploy Dokploy reports done still has to leave the stack in service before it
    # is reported as a success (#629, #691, #698). Health checks that are still
    # starting get this long; a missing or exited container is a verdict at once.
    IN_SERVICE_DEADLINE_SECONDS = 180
    IN_SERVICE_INTERVAL_SECONDS = 5

    @classmethod
    def verify_in_service(cls, c: "Context", compose_id: str) -> str | None:
        """The containers the deploy left behind, not the record Dokploy wrote about it.

        Two proofs, read on the host over ssh (the same channel `check_service` and the
        secret-consumer restart use), both fail-closed:

        - identity: the compose project's checkout (`/etc/dokploy/compose/<appName>/code`)
          is at the ref this deploy pinned — a clone that failed after the record was
          created leaves prod on the previous code with a green receipt (#629);
        - service: every service the compose declares a healthcheck for has a running,
          not-unhealthy container. `Created` (a dependency never became healthy, #698),
          exited, restarting, or absent (#691) is a failure; `health: starting` is waited
          on up to IN_SERVICE_DEADLINE_SECONDS.

        Returns an error string to fail the deploy, None to pass. Skipped with a warning
        only when VPS_HOST is unset (no host to ask); a checkout that cannot resolve the
        ref it pinned fails, since the identity proof is then impossible.

        The identity proof holds only right after THIS checkout deployed the stack; a
        sync that skipped the deploy asks `verify_still_in_service` instead.
        """
        try:
            stack = cls._stack_on_host(c, compose_id)
            if stack is None:
                return None
            identity_error = cls._verify_checkout_identity(stack)
            if identity_error:
                return identity_error
            return cls._verify_containers_in_service(c, stack)
        except _StackUnobservable as exc:
            return str(exc)

    @classmethod
    def verify_still_in_service(cls, c: "Context", compose_id: str) -> str | None:
        """The service half of `verify_in_service`, for a stack sync did not redeploy.

        sync skips a stack whose runtime and source config identities match, so this
        release left it alone and Dokploy's checkout is at the ref of the stack's LAST
        deploy. The identity half would call that a stale clone and force a restart of
        every unchanged service on every release (#718). The config hash already covers
        the compose file, its build context and declared dependencies, so the older
        checkout holds the same inputs; what a skip still has to prove is that the
        containers are up (#691, #698).

        Returns an error string when the stack is not in service (sync redeploys it),
        None when it is. Raises when the host cannot be asked (no appName, `docker ps`
        failed): an unobservable stack is no evidence of a dead one.
        """
        stack = cls._stack_on_host(c, compose_id)
        if stack is None:
            return None
        return cls._verify_containers_in_service(c, stack)

    @staticmethod
    def _on_host(c: "Context", e: dict) -> Callable[[str], Any] | None:
        """Run one shell command on VPS_HOST over ssh; None without a host."""
        host = e.get("VPS_HOST")
        if not host:
            return None
        destination = shlex.quote(f"{e.get('VPS_SSH_USER') or 'root'}@{host}")

        def on_host(command: str) -> Any:
            return c.run(
                f"ssh {destination} {shlex.quote(command)}", hide=True, warn=True
            )

        return on_host

    @classmethod
    def _stack_on_host(cls, c: "Context", compose_id: str) -> _HostStack | None:
        """Where the compose's containers run; None (with a warning) without VPS_HOST."""
        from libs.dokploy import get_dokploy

        e = cls.env()
        on_host = cls._on_host(c, e)
        if on_host is None:
            warning(f"{cls.service}: VPS_HOST unset; skipping the in-service check")
            return None
        domain = e.get("INTERNAL_DOMAIN")
        record = get_dokploy(host=f"cloud.{domain}" if domain else None).get_compose(
            compose_id
        )
        project = str(record.get("appName") or "").strip()
        if not project:
            raise _StackUnobservable(
                f"Dokploy compose {compose_id} reports no appName; its containers cannot be found"
            )
        return _HostStack(host=e["VPS_HOST"], project=project, run=on_host)

    @classmethod
    def restart_dependents(cls, c: "Context", e: dict) -> list[str]:
        """Restart the containers that declared `restart_after` this service (#726).

        Called only after a sync that redeployed this service and proved it in service:
        a recreated Redis starts with an empty Lua script cache, and OpenPanel's
        api/worker (EVALSHA only) and the Authentik worker stay broken until restarted.
        The names come from the registry for this environment (`prod_only` dependents
        only in production), over the same ssh channel as the in-service check.

        A dependent that is not running has no stale connection to shed and is left
        alone (a first deploy of a fresh environment has none yet). Returns the
        containers restarted; raises when the host cannot list or restart them —
        the caller fails the sync, since a retry skips the now-unchanged stack.
        """
        from libs.deploy_dependencies import service_key_from_path
        from libs.service_registry import restart_after_containers

        service_id = service_key_from_path(cls.compose_path or "")
        if not service_id:
            return []  # not a registry service, so nothing can declare restart_after it
        env_name = e.get("ENV", "production")
        declared = restart_after_containers(
            service_id, env_name, e.get("ENV_SUFFIX") or ""
        )
        names = list(dict.fromkeys(n for group in declared.values() for n in group))
        if not names:
            return []
        by_hand = shlex.join(["docker", "restart", *names])
        on_host = cls._on_host(c, e)
        if on_host is None:
            raise RuntimeError(f"VPS_HOST unset; restart by hand: {by_hand}")
        listing = on_host(shlex.join(["docker", "ps", "--format", "{{.Names}}"]))
        if not listing.ok:
            raise RuntimeError(
                f"could not list running containers "
                f"({(listing.stderr or '').strip() or 'docker ps failed'}); "
                f"restart by hand: {by_hand}"
            )
        running = {line.strip() for line in listing.stdout.splitlines()}
        present = [name for name in names if name in running]
        idle = [name for name in names if name not in running]
        if idle:
            warning(
                f"{cls.service}: dependent(s) not running, not restarted: {', '.join(idle)}"
            )
        if not present:
            return []
        restart = on_host(shlex.join(["docker", "restart", *present]))
        if not restart.ok:
            raise RuntimeError(
                f"docker restart failed "
                f"({(restart.stderr or '').strip() or 'no output'}); "
                f"restart by hand: {shlex.join(['docker', 'restart', *present])}"
            )
        success(
            f"{cls.service}: restarted dependents after redeploy ({env_name}): "
            f"{', '.join(present)}"
        )
        return present

    @classmethod
    def _verify_checkout_identity(cls, stack: _HostStack) -> str | None:
        """Dokploy's checkout for the stack is at the ref this checkout pinned (#629)."""
        # Fail-closed: a checkout that cannot say what it pinned cannot prove what Dokploy
        # checked out, and that proof is the point (#629).
        pinned = cls._checkout_ref()
        expected_sha = cls._checkout_sha(pinned) if pinned else None
        if not pinned or not expected_sha:
            return (
                f"this checkout cannot resolve the ref it pinned for {cls.service} "
                f"({pinned or 'no tag or HEAD'}); Dokploy's checkout cannot be proven"
            )
        code_dir = shlex.quote(f"/etc/dokploy/compose/{stack.project}/code")
        head = stack.run(f"git -C {code_dir} rev-parse HEAD")
        deployed_sha = head.stdout.strip() if head.ok else ""
        if deployed_sha != expected_sha:
            return (
                f"Dokploy's checkout for {cls.service} is at "
                f"{deployed_sha[:12] or 'no readable HEAD'} but this deploy pinned "
                f"{pinned} ({expected_sha[:12]}); the record reports done on a stale clone"
            )
        return None

    @classmethod
    def _verify_containers_in_service(
        cls, c: "Context", stack: _HostStack
    ) -> str | None:
        """Every healthchecked service the compose declares has a running, healthy
        container; `health: starting` is waited on up to IN_SERVICE_DEADLINE_SECONDS."""
        from libs.deploy.in_service import (
            DOCKER_PS_FORMAT,
            expected_running_services,
            in_service_verdict,
            parse_docker_ps,
        )

        expected = expected_running_services(cls.get_compose_content(c))
        if not expected:
            return None
        listing = (
            "docker ps -a --filter "
            f"label=com.docker.compose.project={shlex.quote(stack.project)} "
            f"--format {shlex.quote(DOCKER_PS_FORMAT)}"
        )
        deadline = time.monotonic() + cls.IN_SERVICE_DEADLINE_SECONDS
        while True:
            result = stack.run(listing)
            if not result.ok:
                raise _StackUnobservable(
                    f"could not list {stack.project}'s containers on {stack.host}: "
                    f"{result.stderr.strip() or 'docker ps failed'}"
                )
            verdict = in_service_verdict(expected, parse_docker_ps(result.stdout))
            if verdict.ok:
                success(f"{cls.service}: in service — {verdict.message}")
                return None
            if not verdict.settling or time.monotonic() >= deadline:
                return verdict.message
            time.sleep(cls.IN_SERVICE_INTERVAL_SECONDS)

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
        from libs.deploy_queue import deployment_start_epoch
        from libs.deploy.dokploy_adapter import (
            deploy_compose_with_record_check as _deploy_check,
        )

        _deploy_check(
            client,
            compose_id,
            timeout_seconds=cls._resolve_record_timeout(timeout_seconds),
            interval_seconds=cls._resolve_record_interval(interval_seconds),
            wait_fn=lambda c, cid, prev, to, iv, min_s: (
                cls._wait_for_new_deployment_record(
                    c, cid, prev, to, iv, min_started_at=min_s
                )
            ),
            start_epoch_fn=deployment_start_epoch,
            log_warning=warning,
        )

    @staticmethod
    def _deployment_ids(deployments: list[dict]) -> set[str]:
        from libs.deploy.dokploy_adapter import deployment_ids

        return deployment_ids(deployments)

    @staticmethod
    def _get_compose_deployments(client: Any, compose_id: str) -> list[dict]:
        from libs.deploy.dokploy_adapter import get_compose_deployments

        return get_compose_deployments(client, compose_id)

    @staticmethod
    def _started_before_trigger(deployment: dict, floor_epoch: float) -> bool:
        from libs.deploy_queue import deployment_start_epoch
        from libs.deploy.dokploy_adapter import started_before_trigger

        return started_before_trigger(
            deployment,
            floor_epoch,
            start_epoch_fn=deployment_start_epoch,
            clock_skew_tolerance=_CLOCK_SKEW_TOLERANCE_SECONDS,
        )

    _TERMINAL_SUCCESS_STATUSES = frozenset({"done", "success", "successful"})

    @classmethod
    def _wait_for_new_deployment_record(
        cls,
        client: Any,
        compose_id: str,
        previous_ids: set[str],
        timeout_seconds: int,
        interval_seconds: int,
        *,
        min_started_at: float | None = None,
    ) -> bool:
        from libs.deploy_queue import deployment_start_epoch
        from libs.deploy.dokploy_adapter import wait_for_new_deployment_record

        return wait_for_new_deployment_record(
            client,
            compose_id,
            previous_ids,
            timeout_seconds,
            interval_seconds,
            min_started_at=min_started_at,
            start_epoch_fn=deployment_start_epoch,
            terminal_success_statuses=cls._TERMINAL_SUCCESS_STATUSES,
        )

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
    def _prune_stale_dokploy_domains(cls, client: Any, compose_id: str) -> None:
        from libs.deploy.dokploy_adapter import prune_stale_dokploy_domains

        prune_stale_dokploy_domains(
            client, compose_id, log_info=info, log_warning=warning
        )

    @classmethod
    def _find_remote_compose(cls, e: dict[str, str]) -> dict | None:
        from libs.dokploy import get_dokploy
        from libs.deploy.dokploy_adapter import find_remote_compose

        client = get_dokploy()
        return find_remote_compose(
            client,
            cls.service,
            cls.project_name(e),
            env_name=e.get("ENV", "production"),
            legacy_names=getattr(cls, "legacy_compose_names", ()),
        )

    @classmethod
    def get_remote_config_identity(cls) -> dict[str, str | None]:
        from libs.deploy.dokploy_adapter import parse_remote_config_identity

        existing = cls._find_remote_compose(cls.env())
        return parse_remote_config_identity(existing)

    @classmethod
    def get_remote_config_hash(cls) -> str | None:
        """Backward-compatible accessor for the runtime idempotence hash."""
        return cls.get_remote_config_identity()["runtime_hash"]

    @classmethod
    def _resolve_record_timeout(cls, timeout_seconds: int | None = None) -> int:
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
        from libs.deploy.dokploy_adapter import await_effective_config_hash

        return await_effective_config_hash(
            cls.get_remote_config_hash,
            expected_hash,
            cls._resolve_record_timeout(),
            cls._resolve_record_interval(),
        )

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
        from libs.deploy.preflight import check_approle_creds

        compose_text = Path(cls.compose_path).read_text(encoding="utf-8")
        missing = check_approle_creds(compose_text, effective_env)
        if missing:
            e = cls.env()
            cred_missing = [
                key for key in ("VAULT_ROLE_ID", "VAULT_SECRET_ID") if key in missing
            ]
            if cred_missing:
                raise ValueError(
                    f"{cls.service}: compose uses Vault AppRole auth but "
                    f"{', '.join(cred_missing)} is missing from the deploy env — the vault-agent "
                    "would crash-loop on 'VAULT_ROLE_ID and VAULT_SECRET_ID are required'. "
                    f"Run `DEPLOY_ENV={e.get('ENV', 'production')} invoke vault.setup-approle "
                    f"--project {cls.project_name(e)} --service {cls.service} --deploy` before "
                    "deploying."
                )
            if "VAULT_ADDR" in missing:
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
        e = cls.env()
        existing = cls._find_remote_compose(e)

        if not existing:
            return {"valid": True, "error": None, "details": "No existing deployment"}

        from libs.deploy.preflight import (
            extract_vault_token_from_env,
            verify_token_status,
        )

        is_approle, token = extract_vault_token_from_env(existing.get("env", ""))
        if is_approle:
            return {
                "valid": True,
                "error": None,
                "details": "AppRole auth; legacy VAULT_APP_TOKEN preflight skipped",
            }
        if not token:
            return {"valid": True, "error": None, "details": "No VAULT_APP_TOKEN found"}

        vault_addr = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )
        return verify_token_status(
            token, vault_addr=vault_addr, min_ttl_hours=24, verifier=verify_vault_token
        )

    @classmethod
    def validate_preflight_env(cls) -> list[str]:
        """Validate required environment variables."""
        return validate_env()

    @classmethod
    def service_id_from_path(cls) -> str | None:
        """Derive service identity key from compose_path."""
        from libs.deploy_dependencies import service_key_from_path

        return service_key_from_path(cls.compose_path)

    @classmethod
    def sync(cls, c: "Context", force: bool = False) -> dict:
        """Sync IaC state - update only if config changed.

        Returns:
            dict with keys: action (skipped|updated|created|failed), details
        """
        logger = ConsoleLogger(
            header=header,
            info=info,
            warning=warning,
            error=error,
            success=success,
        )
        pipeline = SyncPipeline(deployer=cls, context=c, force=force, logger=logger)
        return pipeline.run()

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
