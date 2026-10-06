"""Declarative synchronization pipeline for service deployment.

Part of libs.deploy domain decomposition (#955, #1009).
Deconstructs procedural deployment synchronization into discrete verifiable steps.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from libs.core.service_identity import ServiceIdentity

if TYPE_CHECKING:
    from invoke import Context

logger = logging.getLogger(__name__)

SOURCE_CONFIG_HASH_VERSION = "v1"
EXACT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class SyncAction:
    """Deployment sync action types aligned with infra2-sdk DeployState.

    ``FAILED`` is a literal, not ``DeployState.FAILED.value``: invoke loads every task
    module, so ``libs.deploy.deployer`` and this module must import without infra2-sdk
    (#1016). ``test_sync_pipeline_sdk_alignment`` pins the literal to the SDK value.
    """

    SKIPPED = "skipped"
    FAILED = "failed"
    CREATED = "created"
    UPDATED = "updated"
    SUPPLIED = "supplied"


class SyncResult(dict):
    """Backward-compatible dictionary mapping for Deployer.sync result."""

    def __init__(self, action: str, details: str, **extra: Any):
        super().__init__(action=action, details=details, **extra)
        self.action = action
        self.details = details

    @property
    def is_success(self) -> bool:
        return self.action != SyncAction.FAILED


@dataclass
class ConsoleLogger:
    """Pluggable console logging callbacks for sync pipeline execution."""

    header: Any = None
    info: Any = None
    warning: Any = None
    error: Any = None
    success: Any = None

    def log_header(self, title: str, subtitle: str = "") -> None:
        if callable(self.header):
            self.header(title, subtitle)
        else:
            logger.info("=== %s: %s ===", title, subtitle)

    def log_info(self, message: str) -> None:
        if callable(self.info):
            self.info(message)
        else:
            logger.info("%s", message)

    def log_warning(self, message: str) -> None:
        if callable(self.warning):
            self.warning(message)
        else:
            logger.warning("%s", message)

    def log_error(self, message: str, details: str = "") -> None:
        if callable(self.error):
            self.error(message, details) if details else self.error(message)
        else:
            logger.error("%s: %s", message, details)

    def log_success(self, message: str) -> None:
        if callable(self.success):
            self.success(message)
        else:
            logger.info("SUCCESS: %s", message)


@dataclass
class SyncContext:
    """State context for a single sync pipeline execution."""

    deployer: Any
    context: Context
    force: bool = False
    logger: ConsoleLogger = field(default_factory=ConsoleLogger)
    env: dict[str, str] = field(default_factory=dict)
    env_vars_dict: dict[str, str] = field(default_factory=dict)
    source_env_vars: dict[str, str] = field(default_factory=dict)
    local_hash: str | None = None
    source_hash: str | None = None
    deploy_ref: str = ""
    service_id: str = ""
    runtime_identity: Any = None
    remote_identity: dict[str, str | None] = field(default_factory=dict)
    remote_hash: str | None = None
    compose_id: str = ""
    restarted_dependents: list[str] = field(default_factory=list)


class SyncPipeline:
    """Declarative execution pipeline for Deployer synchronization."""

    def __init__(
        self,
        deployer: Any,
        context: Context,
        force: bool = False,
        logger: ConsoleLogger | None = None,
    ):
        self.ctx = SyncContext(
            deployer=deployer,
            context=context,
            force=force,
            logger=logger or ConsoleLogger(),
        )

    def run(self) -> SyncResult:
        """Execute all synchronization steps sequentially."""
        preflight_res = self.step_check_preflight()
        if preflight_res is not None:
            return preflight_res

        vault_res = self.step_vault_preflight()
        if vault_res is not None:
            return vault_res

        drift_res = self.step_identities_and_drift()
        if drift_res is not None:
            return drift_res

        deploy_res = self.step_prepare_and_deploy()
        if deploy_res is not None:
            return deploy_res

        return self.step_verify_and_finalize()

    def step_check_preflight(self) -> SyncResult | None:
        """Verify environment pre-conditions and prod-only constraints."""
        deployer = self.ctx.deployer
        log = self.ctx.logger
        log.log_header(f"{deployer.service} sync", "Checking for changes")

        e = deployer.env()
        self.ctx.env = e

        if deployer.prod_only and e.get("ENV", "production") != "production":
            log.log_info(
                f"{deployer.service} is prod-only; skipping {e.get('ENV')} sync"
            )
            return SyncResult(
                action=SyncAction.SKIPPED,
                details=f"prod-only service; not deployed to {e.get('ENV')}",
            )

        missing = deployer.validate_preflight_env()
        if missing:
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Missing env: {', '.join(missing)}",
            )

        if os.environ.get("DEPLOY_ACTION") == "secrets-supply":
            if not deployer.apply_secret_supply(self.ctx.context, env=e.get("ENV")):
                return SyncResult(
                    action=SyncAction.FAILED,
                    details="secret supply left required values missing",
                )
            return SyncResult(
                action=SyncAction.SUPPLIED,
                details="secret supply applied; no compose",
            )

        return None

    def step_vault_preflight(self) -> SyncResult | None:
        """Verify Vault token health and ensure runtime secrets readiness."""
        deployer = self.ctx.deployer
        c = self.ctx.context
        e = self.ctx.env

        try:
            token_status = deployer.verify_vault_app_token()
            if not token_status["valid"]:
                return SyncResult(
                    action=SyncAction.FAILED,
                    details=(
                        f"VAULT_APP_TOKEN issue: {token_status.get('details', 'unknown')}. "
                        "This is a legacy static token; remove it from the service's Dokploy "
                        "env (services authenticate via AppRole now — `invoke vault.setup-approle`)."
                    ),
                )
            if token_status.get("ttl_hours", 999) < 48:
                return SyncResult(
                    action=SyncAction.FAILED,
                    details=(
                        f"VAULT_APP_TOKEN expires in {token_status['ttl_hours']}h. "
                        "It is a legacy static token; remove it from the Dokploy env "
                        "(AppRole services don't use it)."
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Could not verify VAULT_APP_TOKEN: {exc}",
            )

        if not deployer.ensure_runtime_secrets(c):
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Failed to ensure runtime secrets for {deployer.service}",
            )

        if not deployer.apply_secret_supply(c, env=e.get("ENV")):
            return SyncResult(
                action=SyncAction.FAILED,
                details="secret supply left required values missing",
            )

        return None

    def step_identities_and_drift(self) -> SyncResult | None:
        """Compute configuration hashes and detect remote drift."""
        deployer = self.ctx.deployer
        c = self.ctx.context
        e = self.ctx.env
        force = self.ctx.force
        log = self.ctx.logger

        env_vars_dict = deployer.config_env_with_vault_addr(
            deployer.compose_env_base(e), e
        )
        source_env_vars = deployer.config_env_with_vault_addr(
            deployer.source_config_env_base(e), e
        )
        self.ctx.env_vars_dict = env_vars_dict
        self.ctx.source_env_vars = source_env_vars

        local_hash = deployer.compute_local_config_hash(c, env_vars_dict)
        source_hash = f"{SOURCE_CONFIG_HASH_VERSION}:{deployer.compute_local_config_hash(c, source_env_vars)}"
        self.ctx.local_hash = local_hash
        self.ctx.source_hash = source_hash

        deploy_ref = (os.getenv("IAC_DEPLOY_REF") or "").strip().lower()
        if not deploy_ref:
            try:
                res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                deploy_ref = res.stdout.strip().lower() if res.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                deploy_ref = ""

        if not EXACT_COMMIT_RE.fullmatch(deploy_ref):
            return SyncResult(
                action=SyncAction.FAILED,
                details="Deployment identity requires an exact 40-character IAC_DEPLOY_REF",
            )
        self.ctx.deploy_ref = deploy_ref

        service_id = deployer.service_id_from_path()
        if not service_id:
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Could not derive service identity from {deployer.compose_path}",
            )
        self.ctx.service_id = service_id

        runtime_identity = ServiceIdentity.build(
            service_id,
            e.get("ENV", "production"),
            component=deployer.service,
            service_name=deployer.telemetry_service_name or deployer.service,
            version=deploy_ref,
            iac_ref=deploy_ref,
        )
        self.ctx.runtime_identity = runtime_identity

        try:
            remote_identity = deployer.get_remote_config_identity()
            remote_hash = remote_identity["runtime_hash"]
        except Exception as exc:  # noqa: BLE001
            if not force:
                log.log_warning(
                    f"{deployer.service}: remote config hash unreadable ({exc}); "
                    "skipping deploy (fail-closed) to avoid load-amplifying redeploys"
                )
                return SyncResult(
                    action=SyncAction.SKIPPED,
                    details=f"Remote config unreadable; fail-closed: {exc}",
                )
            log.log_warning(
                f"{deployer.service}: remote config unreadable ({exc}); "
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

        self.ctx.remote_identity = remote_identity
        self.ctx.remote_hash = remote_hash

        log.log_info(f"Local config hash: {local_hash}")
        log.log_info(f"Remote config hash: {remote_hash or 'not found'}")

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
            try:
                existing = deployer._find_remote_compose(e)
                in_service_error = (
                    deployer.verify_still_in_service(c, existing["composeId"])
                    if existing and existing.get("composeId")
                    else None
                )
            except Exception as exc:  # noqa: BLE001
                log.log_warning(
                    f"{deployer.service}: could not check the unchanged stack is in "
                    f"service ({exc}); skipping deploy (fail-closed)"
                )
                in_service_error = None

            if in_service_error:
                log.log_warning(
                    f"{deployer.service}: config unchanged but containers not in-service "
                    f"({in_service_error}); forcing redeploy"
                )
            else:
                log.log_success(
                    f"{deployer.service}: config unchanged, skipping deploy"
                )
                return SyncResult(
                    action=SyncAction.SKIPPED,
                    details="Runtime and source config identities match",
                )

        return None

    def step_prepare_and_deploy(self) -> SyncResult | None:
        """Prepare disk directories, inject identity metadata, and trigger deploy."""
        deployer = self.ctx.deployer
        c = self.ctx.context
        e = self.ctx.env
        force = self.ctx.force
        log = self.ctx.logger
        local_hash = self.ctx.local_hash
        remote_hash = self.ctx.remote_hash
        source_hash = self.ctx.source_hash
        deploy_ref = self.ctx.deploy_ref
        runtime_identity = self.ctx.runtime_identity
        env_vars_dict = self.ctx.env_vars_dict

        if remote_hash is None:
            log.log_info("No remote config found, creating new deployment")
        elif force:
            log.log_warning("Force sync requested")
        elif local_hash == remote_hash:
            log.log_info(
                "Runtime config unchanged but release identity is missing or stale; reconciling"
            )
        else:
            log.log_info(f"Config changed ({remote_hash} -> {local_hash}), deploying")

        if not deployer._prepare_dirs(c):
            return SyncResult(
                action=SyncAction.FAILED, details="Failed to prepare directories"
            )

        env_vars_dict["IAC_CONFIG_HASH"] = local_hash or ""
        env_vars_dict["IAC_SOURCE_CONFIG_HASH"] = source_hash or ""
        env_vars_dict["IAC_DEPLOY_REF"] = deploy_ref
        env_vars_dict.update(runtime_identity.deploy_env())
        if deployer.telemetry_service_name:
            telemetry_identity = ServiceIdentity.build(
                self.ctx.service_id,
                e.get("ENV", "production"),
                component=deployer.telemetry_component or deployer.service,
                service_name=deployer.telemetry_service_name,
                version=deploy_ref,
                iac_ref=deploy_ref,
            )
            env_vars_dict["OTEL_SERVICE_NAME"] = telemetry_identity.service_name
            env_vars_dict["OTEL_RESOURCE_ATTRIBUTES"] = (
                telemetry_identity.otel_resource_attributes()
            )

        try:
            compose_id = deployer.composing(c, env_vars_dict)
            self.ctx.compose_id = compose_id
        except Exception as exc:  # noqa: BLE001
            log.log_error(f"Deploy failed: {exc}")
            return SyncResult(action=SyncAction.FAILED, details=str(exc))

        return None

    def step_verify_and_finalize(self) -> SyncResult:
        """Verify effective remote config, container health, and restart dependents."""
        deployer = self.ctx.deployer
        c = self.ctx.context
        e = self.ctx.env
        log = self.ctx.logger
        local_hash = self.ctx.local_hash
        source_hash = self.ctx.source_hash
        deploy_ref = self.ctx.deploy_ref
        runtime_identity = self.ctx.runtime_identity
        compose_id = self.ctx.compose_id
        env_vars_dict = self.ctx.env_vars_dict

        try:
            effective_hash = deployer._await_effective_config_hash(local_hash)
        except Exception as exc:  # noqa: BLE001
            log.log_error(
                f"Post-deploy verification could not read effective config: {exc}"
            )
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Post-deploy verification failed: {exc}",
            )

        if effective_hash != local_hash:
            log.log_error(
                "Post-deploy verification failed: effective IAC_CONFIG_HASH is stale "
                f"(expected {local_hash}, got {effective_hash or 'none'})"
            )
            return SyncResult(
                action=SyncAction.FAILED,
                details=(
                    "Effective remote config is stale after deploy "
                    f"(expected {local_hash}, got {effective_hash or 'none'}); "
                    "runtime may still be running prior config"
                ),
            )

        try:
            effective_identity = deployer.get_remote_config_identity()
        except Exception as exc:  # noqa: BLE001
            log.log_error(f"Post-deploy identity verification failed: {exc}")
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Post-deploy identity verification failed: {exc}",
            )

        expected_deploy_identity = runtime_identity.deploy_env()
        identity_mismatches: list[str] = []
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
            expected_val = expected_deploy_identity[env_key]
            if effective_identity.get(remote_key) != expected_val:
                identity_mismatches.append(
                    f"{env_key} expected {expected_val}, "
                    f"got {effective_identity.get(remote_key) or 'none'}"
                )

        if identity_mismatches:
            details = "; ".join(identity_mismatches)
            log.log_error(f"Post-deploy identity verification failed: {details}")
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Effective remote identity is stale after deploy: {details}",
            )

        runtime_error = deployer.verify_runtime_applied(c, env_vars_dict)
        if runtime_error:
            log.log_error(
                f"{deployer.service}: runtime verification failed: {runtime_error}"
            )
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Runtime verification failed: {runtime_error}",
            )

        try:
            in_service_error = deployer.verify_in_service(c, compose_id)
        except Exception as exc:  # noqa: BLE001
            in_service_error = f"could not verify: {exc}"
        if in_service_error:
            log.log_error(
                f"{deployer.service}: not in service after deploy: {in_service_error}"
            )
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Not in service after deploy: {in_service_error}",
            )

        try:
            restarted = deployer.restart_dependents(c, e)
        except Exception as exc:  # noqa: BLE001
            log.log_error(
                f"{deployer.service}: deployed, but its dependents were not restarted: {exc}"
            )
            return SyncResult(
                action=SyncAction.FAILED,
                details=f"Deployed, but dependents were not restarted: {exc}",
            )

        log.log_success(f"{deployer.service}: deployed with hash {local_hash}")
        result = SyncResult(
            action=SyncAction.UPDATED if self.ctx.remote_hash else SyncAction.CREATED,
            details=f"composeId: {compose_id}",
        )
        if restarted:
            result["restarted_dependents"] = restarted
        return result
