#!/usr/bin/env python3
"""Internal fixed-compose app deploy backend used by deploy_v2.

This module is not the public deploy surface. ``deploy_v2(service, type, version_ref,
iac_ref)`` resolves the coordinate, enforces the data-lane red lines, and then calls this
backend for a bespoke app's fixed staging/prod composes (``libs.deploy.contract.SERVICES``
— finance_report and, since #500, truealpha/app). The only deploy identity this backend
accepts directly is ``service`` + ``env`` plus a resolved app commit/image ref; the data
lane is derived from ``deploy_env_config.EnvConfig.data_default`` for observability and is
not caller-overridable.

The backend mutates a Dokploy compose's env and triggers a deploy. The Dokploy client is
injected so it is unit-testable without a live control plane. It owns fixed-compose
assembly + trigger + readiness/parity: IAC_CONFIG_HASH cache-bust, static infra keys,
model-override passthrough, Vault-token preflight, rollout wait, and post-deploy
effective-config verification.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from libs.core.environ import infra_domain
from libs.deploy.compose_lock import compose_write_lock
from libs.deploy.console import warning
from libs.deploy import schema_gate
from libs.deploy.env_config import app_compose_env_config, otel_env
from libs.deploy.queue import deployment_start_epoch
from libs.core import REPO_ROOT
from libs.deploy.failure_snapshot import emit_failure_snapshot
from libs.observability.openpanel import openpanel_env
from libs.deploy.refs import resolve_to_sha
from libs.deploy.preflight import (
    check_approle_creds,
    extract_vault_token_from_env,
    parse_env_var as _env_value,
    verify_token_status,
)
from libs.deploy.rollout import RolloutError, wait_for_deployment

# infra2#525: Dokploy deployment records carry no caller-supplied correlation id, so a
# start-timestamp floor (captured just before OUR OWN deploy_compose() call) is the only
# available signal that a "new" record belongs to a DIFFERENT, unrelated trigger rather
# than to us. Allow a small margin for clock skew between this process and Dokploy's
# server rather than excluding on an exact instant.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5


@dataclass(frozen=True)
class DeployPlan:
    env: str
    sha: str
    compose_id: str
    data: str  # derived data_lane from EnvConfig, kept for deploy_v2 result detail
    env_vars: dict[str, str]
    # The #698 pre-deploy schema gate's ROLLBACK_CLASS (Infra-022 T3.2 / TODOWRITE:20)
    # for this exact deploy, or None when the gate didn't apply (schema_gate.gate_applies
    # was False for this service). Recorded here so it reaches deploy_v2's own JSON
    # result / the GitHub Actions step summary — an operator deciding whether a later
    # rollback is safe must not have to re-run the check to see this.
    rollback_class: str | None = None


def _dep_id(deployment: dict) -> str:
    return str(deployment.get("deploymentId") or deployment.get("id") or "")


def _deployment_ids(deployments) -> set[str]:
    return {_dep_id(d) for d in (deployments or []) if _dep_id(d)}


def _started_before(deployment: dict, floor_epoch: float) -> bool:
    """True if `deployment` has a parseable start timestamp that is clearly before
    `floor_epoch` (beyond the clock-skew tolerance) — i.e. it cannot be the record our
    own deploy_compose() call produced, because it started before we even called it.

    A record with no parseable startedAt/createdAt/updatedAt is treated as AMBIGUOUS,
    not excluded (returns False) — we only ever use this to rule a record OUT, never to
    rule one in, so an unparseable timestamp preserves prior (pre-infra2#525) behavior
    instead of introducing a new false-negative.
    """
    started = deployment_start_epoch(deployment)
    if started is None:
        return False
    return started < floor_epoch - _CLOCK_SKEW_TOLERANCE_SECONDS


def wait_for_rollout(
    client,
    compose_id: str,
    before_ids: set[str],
    *,
    timeout: int = 600,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
    min_started_at: float | None = None,
) -> dict:
    """Poll until a NEW Dokploy deployment record reaches a terminal-good status.

    Raises RuntimeError if the new record errors, TimeoutError if none finishes in the
    window. Unified with libs.deploy.rollout.wait_for_deployment (D5).
    """
    filter_fn = (
        (lambda d: _started_before(d, min_started_at))
        if min_started_at is not None
        else None
    )
    try:
        result = wait_for_deployment(
            lambda: client.get_compose_deployments(compose_id) or [],
            before_ids,
            timeout_seconds=timeout,
            interval_seconds=interval,
            require_terminal=True,
            raise_on_error=True,
            raise_on_timeout=True,
            is_filtered_fn=filter_fn,
            _sleep=_sleep,
            _now=_now,
        )
        return result.deployment
    except RolloutError as exc:
        raise RuntimeError(
            f"deploy rollout entered error (compose {compose_id})"
        ) from exc
    except TimeoutError as exc:
        raise TimeoutError(
            f"deploy rollout did not finish within {timeout}s (compose {compose_id})"
        ) from exc


def assert_approle_creds_present(service: str, client, compose_id: str) -> None:
    """Fail closed if this service's compose uses Vault AppRole auth but the env about
    to be deployed lacks role/secret/addr creds.

    #290/#316 added this exact guard (the #257/#290 foot-gun: an AppRole config change
    lands without VAULT_ROLE_ID/VAULT_SECRET_ID, so the vault-agent crash-loops instead
    of the service ever starting) — but only on the legacy Deployer.composing() path
    and libs.deploy.preview's own copy. This promote path (what deploy_v2 actually
    routes every app staging/prod deploy through) never carried it over, so a fixed
    compose missing AppRole creds deployed a crash-looping vault-agent with no
    preflight signal. update_compose_env merges into the EXISTING compose env and this
    function's caller never sets these keys, so reading the compose's current env here
    reflects exactly what will still be true after this deploy.
    """
    from libs.core.registry import service_attrs

    meta = service_attrs().get(service)
    if not meta or not meta.compose_path:
        return  # no statically-registered compose file to inspect
    compose_text = (REPO_ROOT / meta.compose_path).read_text(encoding="utf-8")
    env_text = client.get_compose_env(compose_id)
    missing = check_approle_creds(compose_text, env_text)
    if missing:
        raise ValueError(
            f"{service}: compose uses Vault AppRole auth but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing from the deploy env — the "
            "vault-agent would crash-loop (missing role/secret) or hang reaching an "
            "empty address (missing VAULT_ADDR) and deadlock on its healthcheck (~6 "
            f"min) instead of starting. Run `DEPLOY_ENV=<env> invoke vault.setup-approle "
            f"--service={service} --deploy` (or set VAULT_ADDR, e.g. "
            "https://vault.<INTERNAL_DOMAIN>) on the compose/project env before "
            "deploying."
        )


def ensure_generated_secrets(service: str, env: str) -> None:
    """Auto-provision this service's Vault-generated runtime secrets before deploying.

    truealpha#447 root cause: app-web hard-fails on boot without SECRET_KEY
    ("must come from Vault, never the development default"). truealpha's
    Deployer already carries the fix (AppDeployer.ensure_runtime_secrets,
    generates+stores it if missing, idempotent) — but that method is only ever
    invoked from the legacy pre_compose/sync path (invoke <service>.sync via
    the iac-runner). This promote path (what deploy_v2 actually routes every
    app staging/prod deploy through) never called it, so #447 was only fixed
    by a one-off manual Vault seed, not durably: a future Vault wipe/rotation
    for this env would silently ship the same crash-loop again with no
    self-healing. Mirrors assert_approle_creds_present's placement (before any
    compose mutation) but self-heals instead of failing closed — provisioning
    a missing secret is safe to retry/no-op, unlike a hard Vault auth gate.

    Uses libs.deploy.deployer.load_deployer_class (real import, unlike this
    module's other service lookups) because the provisioning logic itself
    — WHICH keys, how they're generated — is app-specific and already lives,
    tested, on each app's own Deployer; duplicating it here as a second
    implementation is exactly the kind of drift this session has been
    finding and removing elsewhere in this repo.

    ensure_runtime_secrets ultimately calls VaultSecrets, which authenticates
    with VAULT_TOKEN (the iac-runner's bounded AppRole token; PR-E) — a
    credential the app-deploy-request receiver (this function's actual real
    caller: a GitHub Actions job reachable via cross-repo repository_dispatch)
    is NOT and must NOT be handed; it only carries DOKPLOY_API_KEY and
    IAC_WEBHOOK_SECRET. So VaultAuthError/VaultConnectionError here mean "this
    execution context cannot check Vault at all," not "the secret write
    failed" — degrade to a warning and let the deploy proceed (exactly
    today's pre-#579 behavior) rather than fail every staging/prod deploy
    closed on a self-heal this context was never going to be able to perform.
    A real write failure (deployer_cls.ensure_runtime_secrets returning False
    with valid Vault access) still raises below — that failure mode remains
    fail-closed.
    """
    from libs.deploy.deployer import load_deployer_class
    from libs.security.store import VaultSecrets

    deployer_cls = load_deployer_class(service)
    if deployer_cls is None:
        return  # no Deployer to consult (e.g. a test double service_id) — nothing to do
    try:
        provisioned = deployer_cls.ensure_runtime_secrets(env=env)
    except (VaultSecrets.VaultAuthError, VaultSecrets.VaultConnectionError) as exc:
        warning(
            f"{service}: skipping generated-secret self-heal for env {env!r} — "
            f"this deploy context has no Vault access ({exc.__class__.__name__}). "
            "Deploying without it, same as before #447's promote.deploy() wiring."
        )
        return
    if not provisioned:
        raise ValueError(
            f"{service}: failed to auto-provision one or more runtime secrets in "
            f"Vault for env {env!r} — see the Vault write error logged above."
        )


def preflight_vault_token(client, compose_id: str, *, min_ttl_hours: int = 48):
    """Fail closed before deploy if the compose's legacy VAULT_APP_TOKEN is present but
    invalid or expiring within min_ttl_hours. The token is gated only when present — a
    compose with no VAULT_APP_TOKEN is left alone (not every compose uses Vault).

    AppRole services (VAULT_ROLE_ID/VAULT_SECRET_ID present) are skipped: post-migration a
    vestigial VAULT_APP_TOKEN can linger in Dokploy and expire un-renewed, so gating on it
    would hard-block an AppRole deploy that would otherwise clean it up. Reuses the
    class-free libs.env.verify_vault_token. This does NOT auto-repair; it fails closed.

    No caller-supplied domain: the token being verified lives on the ONE shared Vault
    instance, never a per-service app-routing domain (infra_domain() — #561's general
    form; this call site was the one #561 didn't cover, since it's phrased as a
    positional caller-supplied param rather than a URL built inline).
    """
    from libs.core.environ import infra_domain

    env_text = client.get_compose_env(compose_id)
    is_approle, token = extract_vault_token_from_env(env_text)
    if is_approle or not token:
        return  # AppRole auth or no token -> nothing to gate on

    result = verify_token_status(
        token, vault_addr=f"https://vault.{infra_domain()}", min_ttl_hours=min_ttl_hours
    )
    if not result.get("valid"):
        raise RuntimeError(
            f"VAULT_APP_TOKEN preflight failed for compose {compose_id}: "
            f"{result.get('error') or 'invalid token'}. This is a legacy static token; "
            "remove it from the service's Dokploy env (services authenticate via AppRole now)."
        )


IN_SERVICE_SETTLE_SECONDS = 180


def verify_in_service(
    client,
    service: str,
    env_suffix: str,
    *,
    timeout: int = IN_SERVICE_SETTLE_SECONDS,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> str:
    """Every service the app's compose declares a healthcheck for has a running,
    not-unhealthy container, seen through Dokploy's ``docker.getContainers`` — the only
    view of the host this tier has (it runs in GitHub Actions, no ssh).

    finance_report v0.1.50 (#698): the backend's migration failed, ``depends_on:
    service_healthy`` left ``finance_report-frontend`` in ``Created``, the site 404'd for
    thirteen minutes — and the rollout record had said done. Missing (Dokploy lists only
    running containers, so ``Created`` and exited show up as absent), restarting or
    unhealthy is a verdict at once; only ``health: starting`` is waited on, up to
    ``timeout``. Returns the verdict message on success; raises RuntimeError otherwise.
    Mirrors libs.deploy.deployer.Deployer.verify_in_service for the iac-runner tier.
    """
    from libs.deploy.in_service import (
        expected_running_containers,
        in_service_verdict,
        observe_containers,
    )
    from libs.core.registry import service_attrs

    meta = service_attrs().get(service)
    if not meta or not meta.compose_path:
        raise RuntimeError(
            f"in-service verify: {service} has no registered compose file to read "
            "expectations from"
        )
    expected = expected_running_containers(
        (REPO_ROOT / meta.compose_path).read_text(encoding="utf-8"), env_suffix
    )
    if not expected:
        raise RuntimeError(
            f"in-service verify: {meta.compose_path} declares no service with a "
            "healthcheck and a container_name; nothing could be proven running"
        )
    deadline = _now() + max(0, timeout)
    while True:
        observed = observe_containers(expected, client.get_containers())
        verdict = in_service_verdict(tuple(sorted(expected)), observed)
        if verdict.ok:
            return verdict.message
        if not verdict.settling or _now() >= deadline:
            raise RuntimeError(
                f"not in service after deploy of {service}{env_suffix or ''}: "
                f"{verdict.message}"
            )
        _sleep(max(1, interval))


def verify_effective_config_hash(
    client,
    compose_id: str,
    expected_hash: str,
    *,
    timeout: int = 600,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> str:
    """Poll the compose's effective IAC_CONFIG_HASH until it matches expected_hash, then
    return it; raise RuntimeError if it never advances within the window. Always returns
    the matched hash on success (never None) — a non-advance is an error, not a value.

    This is the post-deploy "did the config actually roll out" gate. Dokploy applies the
    env update asynchronously, so the effective hash can briefly lag the deploy call;
    polling avoids a false-stale verdict on that settling delay while still failing
    closed if it never advances. A transient read error is tolerated like a non-match and
    retried — polling gives each read an independent chance to clear a Dokploy blip; only
    if no clean read ever lands is the last error surfaced. Mirrors
    libs.deploy.deployer._await_effective_config_hash (compose-id-based rather than by service).
    """
    deadline = _now() + max(0, timeout)
    last_value: str | None = None
    last_error: Exception | None = None
    while True:
        try:
            last_value = _env_value(
                client.get_compose_env(compose_id), "IAC_CONFIG_HASH"
            )
            last_error = None
        except Exception as exc:  # transient Dokploy read; tolerate within window
            last_error = exc
        if last_value == expected_hash:
            return last_value
        if _now() >= deadline:
            if last_error is not None and last_value is None:
                raise RuntimeError(
                    f"post-deploy config verify could not read effective config for "
                    f"compose {compose_id}: {last_error}"
                )
            raise RuntimeError(
                f"post-deploy config verify failed: effective IAC_CONFIG_HASH "
                f"{last_value!r} never advanced to {expected_hash!r} for compose "
                f"{compose_id} within {timeout}s (deploy may not have taken)."
            )
        _sleep(max(1, interval))


def _validate_deploy_preconditions(
    service: str,
    env: str,
    domain: str,
    code: str,
    repo: str | None,
    staging_validated: bool,
    break_glass: bool,
) -> tuple[str, Any, str, str]:
    """Validate deploy inputs and environment requirements before mutation."""
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(
            f"invalid domain {domain!r}: must be non-empty with no whitespace "
            "(it is interpolated into a line-based compose env file)."
        )
    sha = resolve_to_sha(code, repo=repo) if repo is not None else resolve_to_sha(code)
    cfg = app_compose_env_config(service, env)

    if cfg.dynamic:
        raise ValueError(
            f"{env!r} is a per-PR dynamic env with no fixed compose; bind a compose_id "
            "via the preview lifecycle instead of calling deploy() directly."
        )
    if cfg.compose_id is None:
        raise ValueError(
            f"{service!r} has no Dokploy compose registered for env {env!r} "
            "(libs.deploy.env_config._APP_COMPOSE_OVERRIDES) — nothing to deploy to."
        )
    if cfg.requires_staging_first and not staging_validated and not break_glass:
        raise ValueError(
            f"{env!r} requires a staging deploy of digest {sha} first "
            "(promote-not-rebuild). Pass staging_validated=True once staging has run "
            "this exact digest, or break_glass=True as an audited override (H5)."
        )

    deploy_environment = {"prod": "production"}.get(env.strip().lower(), env)
    return sha, cfg, cfg.data_default, deploy_environment


def _preflight_deploy(
    service: str,
    client: Any,
    compose_id: str,
    deploy_environment: str,
    verify_vault: bool,
) -> None:
    """Fail closed on auth or token defects and ensure required secrets exist."""
    assert_approle_creds_present(service, client, compose_id)
    ensure_generated_secrets(service, deploy_environment)
    if verify_vault:
        preflight_vault_token(client, compose_id)


def _evaluate_schema_gate(service: str, cfg: Any, image_tag: str) -> str | None:
    """Run pre-deploy schema gate when applicable to the service."""
    if not schema_gate.gate_applies(service):
        return None
    from libs.core.registry import service_attrs

    meta = service_attrs().get(service)
    if meta is None or not meta.compose_path:
        raise ValueError(
            f"{service}: pre-deploy schema gate is registered for this service but "
            "it has no compose_path to locate its vault-agent container from"
        )
    return schema_gate.run_schema_gate(
        service,
        compose_path=meta.compose_path,
        env_suffix=cfg.env_suffix,
        image_ref=image_tag,
    )


def _build_deploy_env_vars(
    service: str,
    env: str,
    domain: str,
    deploy_environment: str,
    cfg: Any,
    image_tag: str,
    iac_ref: str,
    config_hash: str,
    model_overrides: dict[str, str] | None,
) -> tuple[dict[str, str], Any]:
    """Assemble all environment variables for the deployment compose."""
    env_vars = {
        "IMAGE_TAG": image_tag,
        "GIT_COMMIT_SHA": image_tag,
        "NEXT_PUBLIC_APP_URL": cfg.app_url(domain=domain),
        "ENV_SUFFIX": cfg.env_suffix,
        "ENV_DOMAIN_SUFFIX": cfg.env_suffix,
        "COMPOSE_PROFILES": "app",
        "TRAEFIK_ENABLE": "true",
        "INTERNAL_DOMAIN": domain,
        "IAC_CONFIG_HASH": config_hash,
    }
    from libs.deploy.contract import service_spec
    from libs.core.service_identity import ServiceIdentity

    svc_spec = service_spec(service)
    identity = ServiceIdentity.build(
        service,
        deploy_environment,
        component=svc_spec.identity_component,
        service_name=svc_spec.resolved_identity_service_name(),
        version=image_tag,
        iac_ref=iac_ref,
    )
    env_vars.update(identity.deploy_env())
    env_vars["OTEL_SERVICE_NAME"] = identity.service_name
    env_vars["OTEL_RESOURCE_ATTRIBUTES"] = identity.otel_resource_attributes()
    env_vars.update(openpanel_env(env))
    env_vars.update(otel_env(domain=infra_domain()))
    if model_overrides:
        env_vars.update({k: v for k, v in model_overrides.items() if v})

    from libs.deploy.deployer import load_deployer_class

    deployer_cls = load_deployer_class(service)
    if deployer_cls is not None:
        env_vars.update(
            deployer_cls.compose_env_overrides(
                env=deploy_environment, domain=domain, env_suffix=cfg.env_suffix
            )
        )
    return env_vars, identity


def _execute_dokploy_rollout(
    client: Any,
    compose_id: str,
    service: str,
    env_vars: dict[str, str],
    branch: str | None,
    wait: bool,
    timeout: int,
    config_hash: str,
    verify_config: bool,
    verify_ingestion: bool,
    env_suffix: str,
    service_name: str,
    deploy_environment: str,
    image_tag: str,
) -> None:
    """Execute Dokploy compose deployment under compose write lock."""
    with compose_write_lock(compose_id):
        before_ids = (
            _deployment_ids(client.get_compose_deployments(compose_id))
            if wait
            else set()
        )
        if branch:
            client.update_compose(compose_id, branch=branch)
        client.update_compose_env(compose_id, env_vars=env_vars)
        try:
            trigger_epoch = time.time()
            client.deploy_compose(compose_id)
            if wait:
                wait_for_rollout(
                    client,
                    compose_id,
                    before_ids,
                    timeout=timeout,
                    min_started_at=trigger_epoch,
                )
            if verify_config:
                verify_effective_config_hash(
                    client, compose_id, config_hash, timeout=timeout
                )
            if wait:
                verify_in_service(client, service, env_suffix, timeout=timeout)
            if verify_ingestion:
                from libs.deploy.ingestion_verify import verify_deploy_ingestion

                verify_deploy_ingestion(
                    service_name=service_name,
                    environment=deploy_environment,
                    expected_version=image_tag,
                )
        except Exception:
            emit_failure_snapshot(client, compose_id)
            raise


def deploy(
    env: str,
    code: str,
    *,
    domain: str,
    client,
    service: str = "finance_report/app",
    staging_validated: bool = False,
    break_glass: bool = False,
    repo: str | None = None,
    image_ref: str | None = None,
    iac_ref: str = "",
    branch: str | None = None,
    wait: bool = False,
    timeout: int = 600,
    model_overrides: dict[str, str] | None = None,
    verify_vault: bool = False,
    verify_config: bool = False,
    verify_ingestion: bool = False,
    _now=time.time,
) -> DeployPlan:
    """Deploy a resolved app commit to a fixed app environment.

    code  -> libs.deploy.refs (main / vX.Y.Z / <sha>) -> a commit sha.
    env   -> deploy_env_config (which compose, URL, suffix, default data).
    data  -> derived from the env's data_default; callers cannot override it here.
    """
    sha, cfg, data_lane, deploy_environment = _validate_deploy_preconditions(
        service=service,
        env=env,
        domain=domain,
        code=code,
        repo=repo,
        staging_validated=staging_validated,
        break_glass=break_glass,
    )

    _preflight_deploy(
        service=service,
        client=client,
        compose_id=cfg.compose_id,
        deploy_environment=deploy_environment,
        verify_vault=verify_vault,
    )

    image_tag = image_ref or sha[:7]
    rollback_class = _evaluate_schema_gate(service, cfg, image_tag)

    if cfg.fast_swap and iac_ref:
        config_hash = f"deploy-{image_tag}-{iac_ref[:7]}"
    else:
        config_hash = f"deploy-{image_tag}-{int(_now() * 1000)}"

    env_vars, identity = _build_deploy_env_vars(
        service=service,
        env=env,
        domain=domain,
        deploy_environment=deploy_environment,
        cfg=cfg,
        image_tag=image_tag,
        iac_ref=iac_ref,
        config_hash=config_hash,
        model_overrides=model_overrides,
    )

    _execute_dokploy_rollout(
        client=client,
        compose_id=cfg.compose_id,
        service=service,
        env_vars=env_vars,
        branch=branch,
        wait=wait,
        timeout=timeout,
        config_hash=config_hash,
        verify_config=verify_config,
        verify_ingestion=verify_ingestion,
        env_suffix=cfg.env_suffix,
        service_name=identity.service_name,
        deploy_environment=deploy_environment,
        image_tag=image_tag,
    )

    return DeployPlan(
        env=env,
        sha=sha,
        compose_id=cfg.compose_id,
        data=data_lane,
        env_vars=env_vars,
        rollback_class=rollback_class,
    )


def model_overrides_from_env() -> dict[str, str]:
    """Model overrides supplied via ``DEPLOY_*_MODEL_OVERRIDE`` env vars.

    The staging-E2E promotion path sets these to pin which models prod runs; empty values
    are dropped by ``deploy`` (only non-empty overrides are applied). Consumed by the
    unified ``deploy_v2`` front door so the promote path threads them identically.
    """
    import os

    return {
        "PRIMARY_MODEL": os.getenv("DEPLOY_PRIMARY_MODEL_OVERRIDE", ""),
        "OCR_MODEL": os.getenv("DEPLOY_OCR_MODEL_OVERRIDE", ""),
        "VISION_MODEL": os.getenv("DEPLOY_VISION_MODEL_OVERRIDE", ""),
    }
