"""Unified fixed-compose app rollout, verification, and rollback steps."""

from __future__ import annotations

import base64
from collections.abc import Callable, Collection
from dataclasses import dataclass
import hashlib
import json
import os
import sys
import time
from typing import Any

from common.console import warning
from common.core import registry as _registry
from common.core.environ import infra_domain
from common.core.service_identity import ServiceIdentity
from common.deploy import schema_gate
from common.deploy.app_contract import APP_SOURCES
from common.deploy.compose_compile import (
    ComposeSourceError,
    StackCompose,
    has_x_infra2,
    load_stack,
    vault_template_label,
)
from common.deploy.compose_lock import compose_write_lock
from common.deploy.contract import service_spec, worker_telemetry_env
from common.deploy.dokploy_adapter import (
    await_effective_config_hash,
    deployment_ids as _deployment_ids,
    format_deployment_error_details,
    raw_source_fields,
    read_deployment_log_tail,
    started_before_trigger,
)
from common.deploy.env_config import app_compose_env_config, otel_env
from common.deploy.env_util import RUNTIME_ENV_KEYS_TO_PRESERVE, _parse_env_text
from common.deploy.evidence import VERSION_KEYS, default_http_get, poll_until_healthy
from common.deploy.failure_snapshot import emit_failure_snapshot
from common.deploy.git_refs import resolve_to_sha
from common.deploy.preflight import check_approle_creds, parse_env_var as _env_value
from common.deploy.queue import deployment_start_epoch
from common.deploy.rollout import RolloutError, wait_for_deployment
from common.observability.openpanel import openpanel_env

IN_SERVICE_SETTLE_SECONDS = 180
SMOKE_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class AppRolloutOptions:
    """Policy, ref, and verification flags for fixed-compose SyncPipeline rollout."""

    service: str = "finance_report/app"
    staging_validated: bool = False
    break_glass: bool = False
    force_promote: bool = False
    pause_rollback: bool = False
    max_rollback_attempts: int = 2
    image_ref: str | None = None
    iac_ref: str = ""
    branch: str | None = None
    wait: bool = False
    timeout: int = 600
    verify_config: bool = False
    verify_smoke: bool = True
    verify_ingestion: bool = False


@dataclass(frozen=True)
class AppRolloutDeps:
    """Injectable transport and runtime dependencies for fixed-compose rollout."""

    client: Any = None
    domain: str = ""
    repo: str | None = None
    rollback_notifier: Callable[[dict[str, Any]], None] | None = None
    http_get: Any = None
    now: Callable[[], float] | None = None


@dataclass(frozen=True)
class AppRolloutPlan:
    """Outcome record for one fixed-compose SyncPipeline v2 deployment."""

    env: str
    sha: str
    compose_id: str
    data: str
    env_vars: dict[str, str]
    rollback_class: str | None = None
    force_promote: bool = False
    pause_rollback: bool = False
    max_rollback_attempts: int = 2


class ReleaseTemplateMismatch(ValueError):
    """The compiled Vault template of an x-infra2 app stack differs from the release."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def wait_for_rollout(
    client,
    compose_id: str,
    before_ids: set[str],
    *,
    timeout: int = 600,
    interval: int = 5,
    _sleep=None,
    _now=None,
    min_started_at: float | None = None,
) -> dict:
    """Poll until a new Dokploy deployment record reaches a terminal-good status."""
    sleep_fn = _sleep if _sleep is not None else time.sleep
    now_fn = _now if _now is not None else time.monotonic
    filter_fn = (
        (
            lambda d: started_before_trigger(
                d, min_started_at, start_epoch_fn=deployment_start_epoch
            )
        )
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
            _sleep=sleep_fn,
            _now=now_fn,
        )
        return result.deployment
    except RolloutError as exc:
        dep = getattr(exc, "deployment", None) or {}
        log_tail = read_deployment_log_tail(dep)
        suffix = format_deployment_error_details(dep, log_tail=log_tail)
        raise RuntimeError(
            f"deploy rollout entered error (compose {compose_id}){suffix}"
        ) from exc
    except TimeoutError as exc:
        raise TimeoutError(
            f"deploy rollout did not finish within {timeout}s (compose {compose_id})"
        ) from exc


def assert_approle_creds_present(service: str, client, compose_id: str) -> None:
    """Fail closed if a service compose uses Vault AppRole auth without credentials."""
    meta = _registry.service_attrs().get(service)
    if not meta or not meta.compose_path:
        return
    compose_text = load_stack(meta.compose_path).compiled
    env_text = client.get_compose_env(compose_id)
    missing = check_approle_creds(compose_text, env_text)
    if missing:
        raise ValueError(
            f"{service}: compose uses Vault AppRole auth but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing from the deploy env — the "
            "vault-agent would crash-loop or deadlock on its healthcheck. "
            f"Run `DEPLOY_ENV=<env> invoke vault.setup-approle --service={service} --deploy` "
            "before deploying."
        )


def ensure_generated_secrets(service: str, env: str) -> None:
    """Ensure Vault-generated runtime secrets exist before deploying."""
    from common.deploy.deployer import load_deployer_class
    from common.security.store import VaultSecrets

    deployer_cls = load_deployer_class(service)
    if deployer_cls is None:
        return
    try:
        provisioned = deployer_cls.ensure_runtime_secrets(env=env)
    except (VaultSecrets.VaultAuthError, VaultSecrets.VaultConnectionError) as exc:
        warning(
            f"{service}: skipping generated-secret self-heal for env {env!r} — "
            f"no Vault access in this context ({exc.__class__.__name__})."
        )
        return
    if not provisioned:
        raise ValueError(
            f"{service}: failed to auto-provision one or more runtime secrets in Vault for env {env!r}"
        )


def _normalize_health_get(getter):
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
    """Poll the public /api/health endpoint until HTTP 200, healthy status, and version match."""
    getter = _normalize_health_get(http_get or default_http_get())
    sleep_fn = _sleep if _sleep is not None else time.sleep
    step = max(1.0, float(interval))
    max_attempts = max(1, int(max(0, timeout) // step) + 1)
    poll_until_healthy(
        url,
        http_get=getter,
        expected_version=expected_version,
        version_json_keys=VERSION_KEYS,
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
    _sleep=None,
    _now=None,
) -> str:
    """Verify every service with a healthcheck in the compiled compose is running/healthy."""
    from common.deploy.in_service import (
        expected_running_containers,
        in_service_verdict,
        observe_containers,
    )

    sleep_fn = _sleep if _sleep is not None else time.sleep
    now_fn = _now if _now is not None else time.monotonic

    meta = _registry.service_attrs().get(service)
    if not meta or not meta.compose_path:
        raise RuntimeError(
            f"in-service verify: {service} has no registered compose file"
        )
    expected = expected_running_containers(
        load_stack(meta.compose_path).compiled, env_suffix
    )
    if not expected:
        raise RuntimeError(
            f"in-service verify: {meta.compose_path} declares no service with a "
            "healthcheck and container_name"
        )
    deadline = now_fn() + max(0, timeout)
    while True:
        observed = observe_containers(expected, client.get_containers())
        verdict = in_service_verdict(tuple(sorted(expected)), observed)
        if verdict.ok:
            return verdict.message
        if not verdict.settling or now_fn() >= deadline:
            raise RuntimeError(
                f"not in service after deploy of {service}{env_suffix or ''}: "
                f"{verdict.message}"
            )
        sleep_fn(max(1, interval))


def verify_effective_config_hash(
    client,
    compose_id: str,
    expected_hash: str,
    *,
    expected_keys: Collection[str] | None = None,
    timeout: int = 600,
    interval: int = 5,
    _sleep=None,
    _now=None,
) -> str:
    """Poll effective CONFIGURATION_SHA256 until it matches and check for stale keys."""
    sleep_fn = _sleep if _sleep is not None else time.sleep
    now_fn = _now if _now is not None else time.monotonic
    compose_env = ""

    def _read_hash() -> str | None:
        nonlocal compose_env
        compose_env = client.get_compose_env(compose_id)
        return _env_value(compose_env, "CONFIGURATION_SHA256")

    try:
        last_value = await_effective_config_hash(
            _read_hash,
            expected_hash,
            timeout,
            interval,
            _sleep=sleep_fn,
            _now=now_fn,
        )
    except Exception as exc:
        raise RuntimeError(
            f"post-deploy config verify could not read effective config for "
            f"compose {compose_id}: {exc}"
        ) from exc
    if last_value != expected_hash:
        raise RuntimeError(
            f"post-deploy config verify failed: effective CONFIGURATION_SHA256 "
            f"{last_value!r} never advanced to {expected_hash!r} for compose "
            f"{compose_id} within {timeout}s"
        )
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


def rollout_source_fields(
    service: str, *, branch: str | None, iac_ref: str = ""
) -> dict[str, Any]:
    """Return ``compose.update`` fields selecting compiled raw or GitHub source at ``iac_ref``."""
    from common.deploy import config_drift

    meta = _registry.service_attrs().get(service)
    if not meta or not meta.compose_path:
        return {"branch": branch} if branch else {}
    ref = iac_ref.strip() or "HEAD"
    found = config_drift.contents_at_ref(ref, [meta.compose_path]).get(
        meta.compose_path
    )
    if found is None:
        raise ComposeSourceError(
            f"{service}: {meta.compose_path} is absent at infra2 ref {ref!r}"
        )
    text = found.decode("utf-8")
    if not has_x_infra2(text):
        if not branch:
            return {}
        from common.core.constants import GITHUB_OWNER, GITHUB_REPO

        return {
            "source_type": "github",
            "repository": GITHUB_REPO,
            "owner": GITHUB_OWNER,
            "composePath": meta.compose_path,
            "branch": branch,
        }
    try:
        compiled = config_drift.compiled_at_ref(meta.compose_path, text, ref)
    except ComposeSourceError as exc:
        raise ComposeSourceError(
            f"{service}: cannot compile {meta.compose_path} at infra2 ref {ref!r}: {exc}"
        ) from exc
    stack = StackCompose(source=text, compiled=compiled, x_infra2=True)
    _ = stack.source_type
    compiled_label = vault_template_label(stack.compiled)
    injected = os.environ.get("VAULT_SECRETS_TEMPLATE", "")
    if injected:
        try:
            release = base64.b64decode(injected, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReleaseTemplateMismatch(
                f"{service}: VAULT_SECRETS_TEMPLATE is not base64 UTF-8 text: {exc}"
            ) from None
        if not compiled_label:
            raise ReleaseTemplateMismatch(
                f"{service}: release carries Vault template, but {meta.compose_path}@{ref} has no x-infra2.vault sidecar"
            )
        if _sha256(release) != compiled_label:
            raise ReleaseTemplateMismatch(
                f"{service}: compiled Vault template {compiled_label[:12]} differs from release {_sha256(release)[:12]}"
            )
    return raw_source_fields(stack.compiled)


def validate_fixed_preconditions(
    service: str,
    env: str,
    domain: str,
    code: str,
    repo: str | None,
    staging_validated: bool,
    break_glass: bool,
) -> tuple[str, Any, str, str]:
    """Validate inputs, repo source, compose config, and staging-first gate."""
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(
            f"invalid domain {domain!r}: must be non-empty with no whitespace"
        )
    if repo is None:
        slug = APP_SOURCES.get(service)
        if not slug:
            raise ValueError(
                f"Service {service!r} has no registered source repository in APP_SOURCES"
            )
        effective_repo = f"https://github.com/{slug}.git"
    else:
        effective_repo = repo
    sha = resolve_to_sha(code, repo=effective_repo)
    cfg = app_compose_env_config(service, env)

    if cfg.dynamic:
        raise ValueError(
            f"{env!r} is a per-PR dynamic env with no fixed compose; "
            "deploy through preview mode instead."
        )
    if cfg.compose_id is None:
        raise ValueError(
            f"{service!r} has no Dokploy compose registered for env {env!r}"
        )
    if cfg.requires_staging_first and not staging_validated and not break_glass:
        raise ValueError(
            f"{env!r} requires a staging deploy of digest {sha} first "
            "(pass staging_validated=True or break_glass=True)"
        )
    deploy_environment = {"prod": "production"}.get(env.strip().lower(), env)
    return sha, cfg, cfg.data_default, deploy_environment


def evaluate_schema_gate(
    service: str,
    cfg: Any,
    image_tag: str,
    *,
    force_promote: bool = False,
) -> str | None:
    """Run pre-deploy schema gate when applicable to ``service``."""
    if not schema_gate.gate_applies(service):
        return None
    meta = _registry.service_attrs().get(service)
    if meta is None or not meta.compose_path:
        raise ValueError(
            f"{service}: pre-deploy schema gate registered without compose_path"
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


def build_fixed_env_vars(
    service: str,
    env: str,
    domain: str,
    deploy_environment: str,
    cfg: Any,
    image_tag: str,
    iac_ref: str,
    config_hash: str,
) -> tuple[dict[str, str], ServiceIdentity]:
    """Assemble runtime environment variables and ServiceIdentity for a fixed app rollout."""
    from common.deploy.deployer import load_deployer_class

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
    }
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
    env_vars.update(
        worker_telemetry_env(
            service, deploy_environment, version=image_tag, iac_ref=iac_ref
        )
    )
    env_vars.update(openpanel_env(env))
    env_vars.update(otel_env(domain=infra_domain()))

    deployer_cls = load_deployer_class(service)
    if deployer_cls is not None:
        env_vars.update(
            deployer_cls.compose_env_overrides(
                env=deploy_environment, domain=domain, env_suffix=cfg.env_suffix
            )
        )
    vault_template = os.environ.get("VAULT_SECRETS_TEMPLATE", "")
    if vault_template:
        env_vars["VAULT_SECRETS_TEMPLATE"] = vault_template
    return env_vars, identity


def _notify_rollback_event(
    event: dict[str, Any],
    *,
    rollback_notifier: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Deliver rollback notification without masking the underlying deploy error."""
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

    def send_text() -> None:
        try:
            from common.alerting import deliver_out_of_band_card, text_to_card

            if any(
                os.environ.get(key)
                for key in (
                    "INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL",
                    "FEISHU_WEBHOOK_URL",
                    "INFRA2_OUT_OF_BAND_FEISHU_APP_ID",
                    "FEISHU_APP_ID",
                )
            ):
                deliver_out_of_band_card(
                    os.environ,
                    text_to_card(
                        text, template="red" if severity == "P0" else "orange"
                    ),
                    fallback_text=text,
                )
        except Exception as exc:  # noqa: BLE001
            warning(f"out-of-band rollback alert delivery skipped/failed: {exc}")

    try:
        from common.alerting import stage_card

        stage_card.bounded(send_text, stage_card.ROLLBACK_BUDGET_SECONDS, None)
    except Exception as exc:  # noqa: BLE001
        warning(f"out-of-band rollback alert delivery skipped/failed: {exc}")


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
    """Apply rollback safety brake, Class C gate, circuit breaker, and notification."""
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


def execute_fixed_rollout(
    env: str,
    code: str,
    *,
    options: AppRolloutOptions | None = None,
    deps: AppRolloutDeps | None = None,
) -> AppRolloutPlan:
    """Execute fixed-compose SyncPipeline rollout for an app service."""
    opt = options or AppRolloutOptions()
    dep = deps or AppRolloutDeps()
    if dep.client is None:
        raise ValueError("execute_fixed_rollout requires deps.client")
    now_fn = dep.now if dep.now is not None else time.time
    if opt.force_promote:
        print(
            f"[AUDIT] force_promote=True requested for {opt.service} -> {env} (code={code!r})",
            file=sys.stderr,
        )
    if opt.pause_rollback:
        print(
            f"[AUDIT] pause_rollback=True requested for {opt.service} -> {env} (code={code!r})",
            file=sys.stderr,
        )
    sha, cfg, data_lane, deploy_environment = validate_fixed_preconditions(
        service=opt.service,
        env=env,
        domain=dep.domain,
        code=code,
        repo=dep.repo,
        staging_validated=opt.staging_validated,
        break_glass=opt.break_glass or opt.force_promote,
    )
    assert_approle_creds_present(opt.service, dep.client, cfg.compose_id)
    ensure_generated_secrets(opt.service, deploy_environment)

    image_tag = opt.image_ref or sha[:7]
    rollback_class = evaluate_schema_gate(
        opt.service, cfg, image_tag, force_promote=opt.force_promote
    )
    if cfg.fast_swap and opt.iac_ref:
        config_hash = f"deploy-{image_tag}-{opt.iac_ref[:7]}"
    else:
        config_hash = f"deploy-{image_tag}-{int(now_fn() * 1000)}"

    source_fields = rollout_source_fields(
        opt.service, branch=opt.branch, iac_ref=opt.iac_ref
    )
    env_vars, identity = build_fixed_env_vars(
        service=opt.service,
        env=env,
        domain=dep.domain,
        deploy_environment=deploy_environment,
        cfg=cfg,
        image_tag=image_tag,
        iac_ref=opt.iac_ref,
        config_hash=config_hash,
    )

    compose_id = cfg.compose_id
    app_url = cfg.app_url(domain=dep.domain)
    with compose_write_lock(compose_id):
        existing_env: dict[str, str] = {}
        try:
            existing_env = _parse_env_text(dep.client.get_compose_env(compose_id) or "")
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
            _deployment_ids(dep.client.get_compose_deployments(compose_id))
            if opt.wait
            else set()
        )
        if source_fields:
            dep.client.update_compose(compose_id, **source_fields)
        dep.client.update_compose_env(compose_id, env_vars=env_vars, replace=True)
        try:
            trigger_epoch = time.time()
            dep.client.deploy_compose(compose_id)
            if opt.wait:
                wait_for_rollout(
                    dep.client,
                    compose_id,
                    before_ids,
                    timeout=opt.timeout,
                    min_started_at=trigger_epoch,
                )
            if opt.verify_config:
                verify_effective_config_hash(
                    dep.client,
                    compose_id,
                    config_hash,
                    expected_keys=set(env_vars) | set(RUNTIME_ENV_KEYS_TO_PRESERVE),
                    timeout=opt.timeout,
                )
            if opt.wait:
                verify_in_service(
                    dep.client, opt.service, cfg.env_suffix, timeout=opt.timeout
                )
                if opt.verify_smoke:
                    try:
                        verify_public_health(
                            f"{app_url.rstrip('/')}/api/health",
                            expected_version=image_tag,
                            timeout=opt.timeout,
                            http_get=dep.http_get,
                        )
                    except Exception as smoke_exc:
                        if not opt.force_promote:
                            raise
                        print(
                            f"[AUDIT] force_promote=True: bypassing public /api/health "
                            f"gate for {opt.service} ({image_tag}): {smoke_exc}",
                            file=sys.stderr,
                        )
            if opt.verify_ingestion:
                from common.deploy.ingestion_verify import verify_deploy_ingestion

                verify_deploy_ingestion(
                    service_name=identity.service_name,
                    environment=deploy_environment,
                    expected_version=image_tag,
                )
        except Exception as exc:
            emit_failure_snapshot(dep.client, compose_id)
            _handle_post_deploy_failure(
                exc=exc,
                client=dep.client,
                compose_id=compose_id,
                service=opt.service,
                deploy_environment=deploy_environment,
                image_tag=image_tag,
                previous_image_tag=previous_image_tag,
                previous_rollback_attempts=previous_rollback_attempts,
                env_vars=env_vars,
                rollback_class=rollback_class,
                pause_rollback=opt.pause_rollback,
                max_rollback_attempts=opt.max_rollback_attempts,
                rollback_notifier=dep.rollback_notifier,
            )
            raise

    return AppRolloutPlan(
        env=env,
        sha=sha,
        compose_id=compose_id,
        data=data_lane,
        env_vars=env_vars,
        rollback_class=rollback_class,
        force_promote=opt.force_promote,
        pause_rollback=opt.pause_rollback,
        max_rollback_attempts=opt.max_rollback_attempts,
    )
