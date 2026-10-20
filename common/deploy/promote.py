#!/usr/bin/env python3
"""Internal fixed-compose app deploy backend used by deploy_v2.

This module is not the public deploy surface. ``deploy_v2(service, type, version_ref,
iac_ref)`` resolves the coordinate, enforces the data-lane red lines, and then calls this
backend for a bespoke app's fixed staging/prod composes (``common.deploy.contract.SERVICES``
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

from collections.abc import Callable, Collection
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from common.core.environ import infra_domain
from common.deploy.compose_lock import compose_write_lock
from common.console import warning
from common.deploy import schema_gate
from common.deploy.env_config import app_compose_env_config, otel_env
from common.deploy.env_util import RUNTIME_ENV_KEYS_TO_PRESERVE, _parse_env_text
from common.deploy.queue import deployment_start_epoch
from common.core import REPO_ROOT
from common.deploy.failure_snapshot import emit_failure_snapshot
from common.observability.openpanel import openpanel_env
from common.deploy.refs import resolve_to_sha
from common.deploy.preflight import (
    check_approle_creds,
    extract_vault_token_from_env,
    parse_env_var as _env_value,
    verify_token_status,
)
from common.deploy.rollout import RolloutError, wait_for_deployment
from infra2_sdk.deploy import default_http_get, poll_until_healthy

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
    force_promote: bool = False
    pause_rollback: bool = False
    max_rollback_attempts: int = 2


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
    window. Unified with common.deploy.rollout.wait_for_deployment (D5).
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
    and common.deploy.preview's own copy. This promote path (what deploy_v2 actually
    routes every app staging/prod deploy through) never carried it over, so a fixed
    compose missing AppRole creds deployed a crash-looping vault-agent with no
    preflight signal. update_compose_env preserves RUNTIME_ENV_KEYS_TO_PRESERVE from the
    existing compose env, so reading the compose's current env here reflects whether
    VAULT_ROLE_ID and VAULT_SECRET_ID will be present after this deploy.
    """
    from common.core.registry import service_attrs

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
            "http://vault:8200) on the compose/project env before "
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

    Uses common.deploy.deployer.load_deployer_class (real import, unlike this
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
    from common.deploy.deployer import load_deployer_class
    from common.security.store import VaultSecrets

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
    class-free common.env.verify_vault_token. This does NOT auto-repair; it fails closed.

    No caller-supplied domain: the token being verified lives on the ONE shared Vault
    instance, never a per-service app-routing domain (infra_domain() — #561's general
    form; this call site was the one #561 didn't cover, since it's phrased as a
    positional caller-supplied param rather than a URL built inline).
    """
    from common.core.environ import infra_domain

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
SMOKE_POLL_INTERVAL_SECONDS = 5.0


def _normalize_health_get(getter):
    """Normalize HTTP 200 JSON payloads with status='healthy' to 'ok' for poll_until_healthy."""

    def _wrapped(url: str) -> tuple[int, str]:
        status_code, body = getter(url)
        if status_code == 200 and body:
            try:
                parsed = json.loads(body)
                if (
                    isinstance(parsed, dict)
                    and str(parsed.get("status", "")).lower() == "healthy"
                ):
                    parsed = dict(parsed, status="ok")
                    body = json.dumps(parsed)
            except (ValueError, TypeError):
                pass
        return status_code, body

    return _wrapped


def verify_public_health(
    url: str,
    *,
    expected_version: str,
    timeout: int = IN_SERVICE_SETTLE_SECONDS,
    interval: float = SMOKE_POLL_INTERVAL_SECONDS,
    http_get=None,
    _sleep=None,
) -> None:
    """Poll the public /api/health endpoint until HTTP 200, status in ('ok', 'healthy'), and version matches (#569)."""
    getter = _normalize_health_get(http_get or default_http_get())
    sleep_fn = _sleep if _sleep is not None else time.sleep
    step = max(1.0, float(interval))
    max_attempts = max(1, int(max(0, timeout) // step) + 1)
    poll_until_healthy(
        url,
        http_get=getter,
        expected_version=expected_version,
        version_json_keys=("git_sha", "version"),
        require_status="ok",
        max_attempts=max_attempts,
        interval_seconds=step,
        sleep=sleep_fn,
    )


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
    Mirrors common.deploy.deployer.Deployer.verify_in_service for the iac-runner tier.
    """
    from common.deploy.in_service import (
        expected_running_containers,
        in_service_verdict,
        observe_containers,
    )
    from common.core.registry import service_attrs

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
    expected_keys: Collection[str] | None = None,
    timeout: int = 600,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> str:
    """Poll the compose's effective IAC_CONFIG_HASH until it matches expected_hash, then
    return it; raise RuntimeError if it never advances within the window. Always returns
    the matched hash on success (never None) — a non-advance is an error, not a value.

    When expected_keys is provided, also verify that the effective compose env contains
    no unexpected stale keys once the hash matches.
    """
    deadline = _now() + max(0, timeout)
    last_value: str | None = None
    last_error: Exception | None = None
    compose_env = ""
    while True:
        try:
            compose_env = client.get_compose_env(compose_id)
            last_value = _env_value(compose_env, "CONFIGURATION_SHA256") or _env_value(
                compose_env, "IAC_CONFIG_HASH"
            )
            last_error = None
        except Exception as exc:  # transient Dokploy read; tolerate within window
            last_error = exc
        if last_value == expected_hash:
            if expected_keys is not None:
                effective_keys = set(_parse_env_text(compose_env))
                unexpected = sorted(effective_keys - set(expected_keys))
                if unexpected:
                    raise RuntimeError(
                        f"post-deploy config verify failed: effective config for "
                        f"compose {compose_id} contains unexpected stale keys: "
                        f"{', '.join(unexpected)}"
                    )
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
            "(common.deploy.env_config._APP_COMPOSE_OVERRIDES) — nothing to deploy to."
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


def _evaluate_schema_gate(
    service: str,
    cfg: Any,
    image_tag: str,
    *,
    force_promote: bool = False,
) -> str | None:
    """Run pre-deploy schema gate when applicable to the service."""
    if not schema_gate.gate_applies(service):
        return None
    from common.core.registry import service_attrs

    meta = service_attrs().get(service)
    if meta is None or not meta.compose_path:
        raise ValueError(
            f"{service}: pre-deploy schema gate is registered for this service but "
            "it has no compose_path to locate its vault-agent container from"
        )
    try:
        return schema_gate.run_schema_gate(
            service,
            compose_path=meta.compose_path,
            env_suffix=cfg.env_suffix,
            image_ref=image_tag,
        )
    except schema_gate.SchemaGateError as exc:
        if not force_promote:
            raise
        print(
            f"[AUDIT] force_promote=True: bypassing pre-deploy schema gate for "
            f"{service} ({image_tag}): {exc}",
            file=sys.stderr,
        )
        return "FORCE_PROMOTED"


def _notify_rollback_event(
    event: dict[str, Any],
    *,
    rollback_notifier: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Deliver an immediate rollback notification and never mask the deploy error (#722)."""
    severity = (
        "P0"
        if event.get("circuit_breaker_tripped")
        or event.get("manual_intervention_required")
        else "WARN"
    )
    text = (
        f"[{severity}] Deploy rollback event for {event.get('service')} ({event.get('environment')})\n"
        f"- Reason: {event.get('reason')}\n"
        f"- Version: {event.get('from_version')} -> {event.get('to_version') or 'none'}\n"
        f"- Action: {event.get('action')}\n"
        f"- Manual intervention required: {bool(event.get('manual_intervention_required'))}"
    )
    print(
        f"[AUDIT] rollback notification: {text.replace(chr(10), ' | ')}",
        file=sys.stderr,
    )
    if rollback_notifier is not None:
        try:
            rollback_notifier(event)
        except Exception as exc:  # noqa: BLE001
            warning(f"rollback_notifier callback failed: {exc}")
        return
    try:
        from common.alerting.delivery import deliver_out_of_band_text

        if any(
            os.environ.get(key)
            for key in (
                "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL",
                "FEISHU_WEBHOOK_URL",
                "INFRA2_OUT_OF_BAND_FEISHU_APP_ID",
                "FEISHU_APP_ID",
            )
        ):
            deliver_out_of_band_text(os.environ, text)
    except Exception as exc:  # noqa: BLE001
        warning(f"out-of-band rollback alert delivery skipped/failed: {exc}")


def _build_deploy_env_vars(
    service: str,
    env: str,
    domain: str,
    deploy_environment: str,
    cfg: Any,
    image_tag: str,
    iac_ref: str,
    config_hash: str,
) -> tuple[dict[str, str], Any]:
    """Assemble all environment variables for the deployment compose."""
    env_vars = {
        "ENV": deploy_environment,
        "VAULT_ADDR": "http://vault:8200",
        "IMAGE_TAG": image_tag,
        "GIT_COMMIT_SHA": image_tag,
        "NEXT_PUBLIC_APP_URL": cfg.app_url(domain=domain),
        "ENV_SUFFIX": cfg.env_suffix,
        "ENV_DOMAIN_SUFFIX": cfg.env_suffix,
        "COMPOSE_PROFILES": "app",
        "TRAEFIK_ENABLE": "true",
        "INTERNAL_DOMAIN": domain,
        "CONFIGURATION_SHA256": config_hash,
        "IAC_CONFIG_HASH": config_hash,
    }
    from common.deploy.contract import service_spec
    from common.core.service_identity import ServiceIdentity

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

    from common.deploy.deployer import load_deployer_class

    deployer_cls = load_deployer_class(service)
    if deployer_cls is not None:
        env_vars.update(
            deployer_cls.compose_env_overrides(
                env=deploy_environment, domain=domain, env_suffix=cfg.env_suffix
            )
        )
    return env_vars, identity


def _handle_post_deploy_failure(
    *,
    exc: Exception,
    client: Any,
    compose_id: str,
    service: str,
    deploy_environment: str,
    image_tag: str,
    previous_image_tag: str | None,
    previous_rollback_attempts: int,
    env_vars: dict[str, str],
    rollback_class: str | None,
    pause_rollback: bool,
    max_rollback_attempts: int,
    rollback_notifier: Callable[[dict[str, Any]], None] | None,
) -> None:
    """Apply rollback safety brake, circuit breaker, automatic rollback, and notification (#722)."""
    reason = str(exc)
    if pause_rollback:
        print(
            f"[AUDIT] pause_rollback=True: suppressing automatic rollback for "
            f"{service} ({deploy_environment}) after failure: {reason}",
            file=sys.stderr,
        )
        _notify_rollback_event(
            {
                "service": service,
                "environment": deploy_environment,
                "reason": reason,
                "from_version": image_tag,
                "to_version": previous_image_tag,
                "action": "paused",
                "pause_rollback": True,
                "circuit_breaker_tripped": False,
                "manual_intervention_required": True,
            },
            rollback_notifier=rollback_notifier,
        )
        return
    if rollback_class == "C":
        _notify_rollback_event(
            {
                "service": service,
                "environment": deploy_environment,
                "reason": f"ROLLBACK_CLASS C forbids automatic rollback: {reason}",
                "from_version": image_tag,
                "to_version": previous_image_tag,
                "action": "blocked_by_schema_class_c",
                "circuit_breaker_tripped": True,
                "manual_intervention_required": True,
            },
            rollback_notifier=rollback_notifier,
        )
        return
    if not previous_image_tag or previous_image_tag == image_tag:
        return
    attempt = previous_rollback_attempts + 1
    if attempt > max_rollback_attempts:
        _notify_rollback_event(
            {
                "service": service,
                "environment": deploy_environment,
                "reason": (
                    f"rollback circuit breaker tripped (attempt {attempt} > "
                    f"max_rollback_attempts {max_rollback_attempts}): {reason}"
                ),
                "from_version": image_tag,
                "to_version": previous_image_tag,
                "action": "circuit_breaker_tripped",
                "attempt": attempt,
                "max_rollback_attempts": max_rollback_attempts,
                "circuit_breaker_tripped": True,
                "manual_intervention_required": True,
                "severity": "P0",
            },
            rollback_notifier=rollback_notifier,
        )
        raise RuntimeError(
            f"automatic rollback circuit breaker tripped for {service} ({deploy_environment}): "
            f"attempt {attempt} exceeds max_rollback_attempts={max_rollback_attempts}; "
            f"manual intervention required (P0 alert emitted): {reason}"
        ) from exc

    rollback_env = dict(env_vars)
    rollback_env["IMAGE_TAG"] = previous_image_tag
    rollback_env["GIT_COMMIT_SHA"] = previous_image_tag
    rollback_env["INFRA_SERVICE_VERSION"] = previous_image_tag
    rollback_env["ROLLBACK_ATTEMPTS"] = str(attempt)
    rollback_action = "rolled_back"
    manual_required = False
    try:
        client.update_compose_env(compose_id, env_vars=rollback_env, replace=True)
        client.deploy_compose(compose_id)
    except Exception as rb_exc:  # noqa: BLE001
        rollback_action = f"rollback_failed: {rb_exc}"
        manual_required = True
    _notify_rollback_event(
        {
            "service": service,
            "environment": deploy_environment,
            "reason": reason,
            "from_version": image_tag,
            "to_version": previous_image_tag,
            "action": rollback_action,
            "attempt": attempt,
            "max_rollback_attempts": max_rollback_attempts,
            "circuit_breaker_tripped": False,
            "manual_intervention_required": manual_required,
        },
        rollback_notifier=rollback_notifier,
    )


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
    verify_smoke: bool,
    verify_ingestion: bool,
    env_suffix: str,
    service_name: str,
    deploy_environment: str,
    image_tag: str,
    app_url: str,
    http_get: Any = None,
    *,
    rollback_class: str | None = None,
    force_promote: bool = False,
    pause_rollback: bool = False,
    max_rollback_attempts: int = 2,
    rollback_notifier: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Execute Dokploy compose deployment under compose write lock."""
    with compose_write_lock(compose_id):
        existing_env: dict[str, str] = {}
        try:
            existing_env = _parse_env_text(client.get_compose_env(compose_id) or "")
        except Exception:  # noqa: BLE001
            existing_env = {}
        previous_image_tag = existing_env.get("IMAGE_TAG")
        try:
            previous_rollback_attempts = int(
                existing_env.get("ROLLBACK_ATTEMPTS") or "0"
            )
        except ValueError:
            previous_rollback_attempts = 0

        before_ids = (
            _deployment_ids(client.get_compose_deployments(compose_id))
            if wait
            else set()
        )
        if branch:
            client.update_compose(compose_id, branch=branch)
        client.update_compose_env(compose_id, env_vars=env_vars, replace=True)
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
                    client,
                    compose_id,
                    config_hash,
                    expected_keys=set(env_vars) | set(RUNTIME_ENV_KEYS_TO_PRESERVE),
                    timeout=timeout,
                )
            if wait:
                verify_in_service(client, service, env_suffix, timeout=timeout)
                if verify_smoke:
                    try:
                        verify_public_health(
                            f"{app_url.rstrip('/')}/api/health",
                            expected_version=image_tag,
                            timeout=timeout,
                            http_get=http_get,
                        )
                    except Exception as smoke_exc:
                        if not force_promote:
                            raise
                        print(
                            f"[AUDIT] force_promote=True: bypassing public /api/health "
                            f"gate for {service} ({image_tag}): {smoke_exc}",
                            file=sys.stderr,
                        )
            if verify_ingestion:
                from common.deploy.ingestion_verify import verify_deploy_ingestion

                verify_deploy_ingestion(
                    service_name=service_name,
                    environment=deploy_environment,
                    expected_version=image_tag,
                )
        except Exception as exc:
            emit_failure_snapshot(client, compose_id)
            _handle_post_deploy_failure(
                exc=exc,
                client=client,
                compose_id=compose_id,
                service=service,
                deploy_environment=deploy_environment,
                image_tag=image_tag,
                previous_image_tag=previous_image_tag,
                previous_rollback_attempts=previous_rollback_attempts,
                env_vars=env_vars,
                rollback_class=rollback_class,
                pause_rollback=pause_rollback,
                max_rollback_attempts=max_rollback_attempts,
                rollback_notifier=rollback_notifier,
            )
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
    force_promote: bool = False,
    pause_rollback: bool = False,
    max_rollback_attempts: int = 2,
    rollback_notifier: Callable[[dict[str, Any]], None] | None = None,
    repo: str | None = None,
    image_ref: str | None = None,
    iac_ref: str = "",
    branch: str | None = None,
    wait: bool = False,
    timeout: int = 600,
    verify_vault: bool = False,
    verify_config: bool = False,
    verify_smoke: bool = True,
    verify_ingestion: bool = False,
    http_get: Any = None,
    _now=time.time,
) -> DeployPlan:
    """Deploy a resolved app commit to a fixed app environment.

    code  -> common.deploy.refs (main / vX.Y.Z / <sha>) -> a commit sha.
    env   -> deploy_env_config (which compose, URL, suffix, default data).
    data  -> derived from the env's data_default; callers cannot override it here.
    """
    if force_promote:
        print(
            f"[AUDIT] force_promote=True requested for {service} -> {env} (code={code!r})",
            file=sys.stderr,
        )
    if pause_rollback:
        print(
            f"[AUDIT] pause_rollback=True requested for {service} -> {env} (code={code!r})",
            file=sys.stderr,
        )
    sha, cfg, data_lane, deploy_environment = _validate_deploy_preconditions(
        service=service,
        env=env,
        domain=domain,
        code=code,
        repo=repo,
        staging_validated=staging_validated,
        break_glass=break_glass or force_promote,
    )

    _preflight_deploy(
        service=service,
        client=client,
        compose_id=cfg.compose_id,
        deploy_environment=deploy_environment,
        verify_vault=verify_vault,
    )

    image_tag = image_ref or sha[:7]
    rollback_class = _evaluate_schema_gate(
        service,
        cfg,
        image_tag,
        force_promote=force_promote,
    )

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
        verify_smoke=verify_smoke,
        verify_ingestion=verify_ingestion,
        env_suffix=cfg.env_suffix,
        service_name=identity.service_name,
        deploy_environment=deploy_environment,
        image_tag=image_tag,
        app_url=cfg.app_url(domain=domain),
        http_get=http_get,
        rollback_class=rollback_class,
        force_promote=force_promote,
        pause_rollback=pause_rollback,
        max_rollback_attempts=max_rollback_attempts,
        rollback_notifier=rollback_notifier,
    )

    return DeployPlan(
        env=env,
        sha=sha,
        compose_id=cfg.compose_id,
        data=data_lane,
        env_vars=env_vars,
        rollback_class=rollback_class,
        force_promote=force_promote,
        pause_rollback=pause_rollback,
        max_rollback_attempts=max_rollback_attempts,
    )
