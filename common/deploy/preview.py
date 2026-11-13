#!/usr/bin/env python3
"""Preview environment lifecycle management for dynamic multi-alias stacks."""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx
from infra2_sdk.deploy import poll_until_healthy
from infra2_sdk.routing import PreviewStackCoordinates, derive_preview_coordinates

from common.core.environ import infra_domain
from common.deploy.app_contract import APP_SOURCES
from common.deploy.compose_lock import compose_write_lock
from common.deploy.dokploy_adapter import deployment_ids as _deployment_ids
from common.deploy.env_util import _parse_env_text
from common.deploy.promote import wait_for_rollout
from common.deploy.env_config import (
    PREVIEW_ENVIRONMENT,
    PreviewAlias,
    PreviewReadinessProbe,
    otel_env,
    preview_alias,
    preview_service_config,
)
from common.observability.openpanel import openpanel_env
from infra2_sdk.refs import resolve_to_sha

PreviewType = Literal["branch", "pr"]


@dataclass(frozen=True)
class PreviewResult:
    action: str  # "up" | "down"
    alias: str
    compose_id: str | None
    sha: str | None
    url: str
    healthy: bool | None  # None when no health check was performed (down / --no-wait)
    containers_cleared: bool | None = None  # set by down(); None for up()


# The source env's app compose to read AppRole creds from (its name in Dokploy).
_SOURCE_APP_COMPOSE = "app"
# The runtime AppRole creds the preview vault-agent logs in with. Preview reads the source
# env's secret path, so it reuses that env's role verbatim — the same creds staging itself
# runs with (injected once by `invoke setup-approle`); see _source_app_vault_creds.
_VAULT_CRED_KEYS = ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID")
_AMBIGUOUS_CREATE_RECOVERY_ATTEMPTS = 4
_AMBIGUOUS_CREATE_RECOVERY_INTERVAL_SECONDS = 2


def _source_app_vault_creds(client, project: str, source_env: str) -> dict[str, str]:
    """Read AppRole credentials from the source environment app compose."""
    comp = client.find_compose_by_name(
        _SOURCE_APP_COMPOSE, project, env_name=source_env
    )
    if not comp:
        raise RuntimeError(
            f"cannot source preview Vault creds: no {_SOURCE_APP_COMPOSE!r} compose in "
            f"{project}/{source_env}"
        )
    parsed = _parse_env_text(client.get_compose_env(comp["composeId"]) or "")
    creds = {k: parsed[k] for k in _VAULT_CRED_KEYS if parsed.get(k)}
    missing = [k for k in _VAULT_CRED_KEYS if k not in creds]
    if missing:
        raise RuntimeError(
            f"{source_env} {_SOURCE_APP_COMPOSE!r} compose is missing Vault creds "
            f"{missing}; run `invoke setup-approle --project {project}` first"
        )
    return creds


def render_throwaway_secrets(config) -> dict[str, str]:
    """Generate one fresh value for each throwaway secret a Preview config names.

    Each call returns new values, so two slots never share a secret. Values are
    ``token_urlsafe`` strings: safe in a URL, a shell word and a SQL literal.
    """
    return {name: secrets.token_urlsafe(24) for name in config.throwaway_secrets}


def _validate_domain(domain: str) -> str:
    """Reject malformed domains with whitespace or empty string."""
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(f"invalid domain {domain!r}: non-empty, no whitespace")
    return domain


def _preview_coordinates(
    service: str, alias: PreviewAlias, domain: str | None = None
) -> PreviewStackCoordinates:
    config = preview_service_config(service)
    return derive_preview_coordinates(
        config.base_subdomain or service.split("/")[-1],
        alias.deployment_environment,
        base_domain=domain,
    )


def _preview_env_vars(
    alias: PreviewAlias,
    *,
    service: str,
    sha: str,
    domain: str,
    image_ref: str | None = None,
    iac_ref: str = "",
    _now=time.time,
) -> dict[str, str]:
    """Assemble Dokploy compose environment variables for one preview alias."""
    config = preview_service_config(service)
    coords = _preview_coordinates(service, alias, domain=domain)
    # IMAGE_TAG is the published ref: a release tag (image_ref="vX.Y.Z") or the short sha.
    image_tag = image_ref or sha[:7]
    config_hash = f"preview-{image_tag}-{int(_now() * 1000)}"  # per-deploy cache-bust
    env_vars = {
        "IMAGE_TAG": image_tag,
        "GIT_COMMIT_SHA": image_tag,
        "NEXT_PUBLIC_APP_URL": alias.app_url(
            domain=domain, base_subdomain=config.base_subdomain
        ),
        # #368: FE OTLP endpoint from the ONE source (consumed, not re-built in compose).
        # infra_domain(), not this app's own `domain` — SigNoz/OTel is the ONE shared
        # collector (SHARED_PLATFORM_SERVICES), never per-service-domain-overridden;
        # promote.deploy() had this exact bug fixed already (#561's general form) —
        # preview.up() carried the same bug, just never observed (a throwaway preview's
        # telemetry silently no-ops rather than crash-looping like #561's prod symptom).
        **otel_env(domain=infra_domain()),
        # #375: every preview alias (main / pr-<N> / commit-<sha7>) shares the single
        # "preview" OpenPanel project; inject its client-id at runtime so preview
        # analytics actually emits (alias granularity rides deployment.environment, not
        # a per-alias project). staging/prod get this via common.deploy.promote; preview was
        # the missing path — without it OPENPANEL_CLIENT_ID stayed empty and analytics
        # silently no-op'd on every preview.
        **openpanel_env("preview"),
        "TIER_CPU_SHARES": str(coords.cpu_shares),
        "ENV_SUFFIX": coords.env_suffix,
        "ENV_DOMAIN_SUFFIX": coords.env_domain_suffix,
        # ENV is the alias display label so the telemetry identity contract
        # (core.environments §4.5: deployment.environment = pr-<N>/commit-<sha>/main)
        # is carried into the running stack.
        "ENV": alias.deployment_environment,
        # Vault app-secrets source env (preview has no per-alias Vault path).
        "COMPOSE_PROFILES": "app",
        "TRAEFIK_ENABLE": "true",
        "INTERNAL_DOMAIN": domain,
        "CONFIGURATION_SHA256": config_hash,
        "IAC_CONFIG_HASH": config_hash,
    }
    vault_template = os.environ.get("VAULT_SECRETS_TEMPLATE", "")
    if vault_template and not config.throwaway_secrets:
        env_vars["VAULT_SECRETS_TEMPLATE"] = vault_template
    from common.deploy.contract import service_spec
    from common.core.service_identity import ServiceIdentity

    spec = service_spec(service)
    identity = ServiceIdentity.build(
        service,
        alias.deployment_environment,
        component=spec.identity_component,
        service_name=spec.resolved_identity_service_name(),
        version=image_tag,
        iac_ref=iac_ref,
    )
    env_vars.update(identity.deploy_env())
    env_vars["OTEL_SERVICE_NAME"] = identity.service_name
    env_vars["OTEL_RESOURCE_ATTRIBUTES"] = identity.otel_resource_attributes()
    return env_vars


def _find_compose(client, project: str, name: str):
    """Return the existing preview compose dict for ``name`` in ``project``, or None."""
    return client.find_compose_by_name(name, project, env_name=PREVIEW_ENVIRONMENT)


def _find_compose_for_down(client: Any, project: str, alias: Any) -> dict | None:
    """Locate a preview compose for teardown, including legacy bare alias names."""
    found = _find_compose(client, project, alias.compose_name)
    if found is not None:
        return found
    envs = (
        (PREVIEW_ENVIRONMENT, "test")
        if alias.alias.startswith("pr-")
        else (PREVIEW_ENVIRONMENT,)
    )
    for env_name in envs:
        found = client.find_compose_by_name(alias.alias, project, env_name=env_name)
        if found is not None:
            return found
    return None


def _ensure_preview_compose(
    client,
    config: Any,
    alias: Any,
    _sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Find existing preview compose or create a new one with retry recovery."""
    existing = _find_compose(client, config.project, alias.compose_name)
    if existing:
        return existing["composeId"]

    try:
        env_obj, _ = client.ensure_environment(config.project, PREVIEW_ENVIRONMENT)
    except ValueError as exc:
        raise RuntimeError(
            f"could not ensure Dokploy environment "
            f"{config.project!r}/{PREVIEW_ENVIRONMENT!r}: {exc}"
        ) from exc
    environment_id = env_obj.get("environmentId")
    if not environment_id:
        raise RuntimeError(
            f"could not ensure Dokploy environment "
            f"{config.project!r}/{PREVIEW_ENVIRONMENT!r}"
        )

    try:
        created = client.create_compose(
            environment_id=environment_id,
            name=alias.compose_name,
            app_name=alias.compose_name,
            source_type="github",
        )
    except httpx.HTTPError:
        reconciled = None
        for attempt in range(_AMBIGUOUS_CREATE_RECOVERY_ATTEMPTS):
            try:
                reconciled = _find_compose(client, config.project, alias.compose_name)
            except httpx.HTTPError:
                reconciled = None
            if reconciled:
                break
            if attempt < _AMBIGUOUS_CREATE_RECOVERY_ATTEMPTS - 1:
                _sleep(_AMBIGUOUS_CREATE_RECOVERY_INTERVAL_SECONDS)
        if reconciled is None:
            raise
        return reconciled["composeId"]

    return created["composeId"]


def up(
    kind: str,
    value: int | str | None,
    *,
    code: str,
    domain: str,
    client,
    service: str = "finance_report/app",
    image_ref: str | None = None,
    iac_ref: str = "",
    github_id: str | None = None,
    repo_owner: str = "wangzitian0",
    repo_name: str = "infra2",
    branch: str = "main",
    wait: bool = True,
    health_timeout: int = 600,
    health_interval: int = 10,
    repo: str | None = None,
    http_get=None,
    _now=time.time,
    _sleep=time.sleep,
    _monotonic=time.monotonic,
) -> PreviewResult:
    """Deploy code to the preview stack for one alias."""
    _validate_domain(domain)
    config = preview_service_config(service)

    alias = preview_alias(kind, value, slug_prefix=config.slug_prefix)
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
    env_vars = _preview_env_vars(
        alias,
        service=service,
        sha=sha,
        domain=domain,
        image_ref=image_ref,
        iac_ref=iac_ref,
        _now=_now,
    )
    if config.throwaway_secrets:
        # A platform Preview never reads Vault: the slot gets its own secrets, and they
        # die with the slot. Nothing is printed or written to the repository.
        env_vars.update(render_throwaway_secrets(config))
    else:
        env_vars.update(
            _source_app_vault_creds(client, config.project, config.secret_env)
        )

    gh_id = github_id if github_id is not None else client.get_github_provider_id()
    if not gh_id:
        raise RuntimeError(
            "no GitHub provider configured in Dokploy; add one in Settings -> Git "
            "Providers before deploying a preview (the compose template is pulled from "
            "the infra2 repo, not uploaded raw)."
        )
    source_fields = dict(
        source_type="github",
        githubId=gh_id,
        repository=repo_name,
        owner=repo_owner,
        branch=branch,
        composePath=config.compose_path,
        autoDeploy=False,
    )

    had_existing_compose = (
        _find_compose(client, config.project, alias.compose_name) is not None
    )
    compose_id = _ensure_preview_compose(
        client=client, config=config, alias=alias, _sleep=_sleep
    )

    healthy: bool | None = None
    url = alias.app_url(domain=domain, base_subdomain=config.base_subdomain)
    expected_versions = {
        "runtime": image_ref or sha[:7],
        "source": sha[:7],
    }

    # Serialize the full read-modify-write-trigger-observe sequence for this compose.
    # The workflow concurrency group covers separate CI runners; this lock also protects
    # direct/in-process callers from losing an env update or accepting another trigger's
    # deployment record (same boundary as fixed-compose promotion, infra2#525).
    with compose_write_lock(compose_id):
        before_ids = (
            _deployment_ids(client.get_compose_deployments(compose_id))
            if wait
            else set()
        )

        # Configure source + env on BOTH paths. Dokploy's compose.create persists ONLY
        # basic fields, so github binding and env must be re-applied before every deploy.
        # update_compose_env with replace=True replaces stale keys while preserving runtime AppRole credentials.
        client.update_compose(compose_id, **source_fields)
        client.update_compose_env(compose_id, env_vars=env_vars, replace=True)

        # A public 200 may still come from the old stack. Snapshot before this trigger and
        # require a new deployment record to reach terminal-good before probing routes.
        trigger_epoch = _now()
        client.deploy_compose(compose_id)
        if wait:
            try:
                wait_for_rollout(
                    client,
                    compose_id,
                    before_ids,
                    timeout=health_timeout,
                    interval=health_interval,
                    _sleep=_sleep,
                    min_started_at=trigger_epoch,
                )
            except RuntimeError as exc:
                if (
                    not had_existing_compose
                    or "deploy rollout entered error" not in str(exc)
                ):
                    raise
                client.delete_compose(compose_id, delete_volumes=True)
                if callable(getattr(client, "get_containers", None)):
                    wait_for_containers_absent(
                        client,
                        service,
                        alias.env_suffix,
                        _sleep=_sleep,
                    )
                compose_id = _ensure_preview_compose(
                    client=client, config=config, alias=alias, _sleep=_sleep
                )
                before_ids = _deployment_ids(client.get_compose_deployments(compose_id))
                client.update_compose(compose_id, **source_fields)
                client.update_compose_env(compose_id, env_vars=env_vars, replace=True)
                trigger_epoch = _now()
                client.deploy_compose(compose_id)
                wait_for_rollout(
                    client,
                    compose_id,
                    before_ids,
                    timeout=health_timeout,
                    interval=health_interval,
                    _sleep=_sleep,
                    min_started_at=trigger_epoch,
                )
            healthy = _wait_for_readiness(
                url,
                config.readiness_probes,
                expected_versions=expected_versions,
                timeout=health_timeout,
                interval=health_interval,
                http_get=http_get,
                _sleep=_sleep,
                _now=_monotonic,
            )
            if not healthy:
                raise TimeoutError(
                    f"preview {alias.alias} did not serve the requested deployment "
                    f"identity on every public surface within {health_timeout}s"
                )

    return PreviewResult(
        action="up",
        alias=alias.alias,
        compose_id=compose_id,
        sha=sha,
        url=url,
        healthy=healthy,
    )


def check_containers_absent(
    client, service: str, slot_suffix: str
) -> tuple[bool, list[str]]:
    """Inspect Dokploy container inventory for surviving containers matching slot_suffix.

    Returns:
        (True, []) if no matching containers exist, or (False, survivors) if
        matching containers remain.
    """
    service_project = service.split("/", 1)[0]
    project_hyphen = service_project.replace("_", "-")
    containers = client.get_containers()
    if not isinstance(containers, list) or not all(
        isinstance(row, dict) and isinstance(row.get("name"), str) for row in containers
    ):
        raise RuntimeError("Dokploy container inventory is not readable")
    survivors = sorted(
        row["name"]
        for row in containers
        if (
            row["name"].lstrip("/").startswith(f"{service_project}-")
            or row["name"].lstrip("/").startswith(f"{project_hyphen}-")
        )
        and row["name"].endswith(slot_suffix)
    )
    return (not bool(survivors), survivors)


def wait_for_containers_absent(
    client,
    service: str,
    slot_suffix: str,
    *,
    attempts: int = 6,
    _sleep=None,
) -> bool:
    """Poll container absence until two consecutive clean reads occur."""
    sleep = _sleep or time.sleep
    consecutive_absent = 0
    for i in range(attempts):
        try:
            absent, _ = check_containers_absent(client, service, slot_suffix)
            if absent:
                consecutive_absent += 1
                if consecutive_absent >= 2:
                    return True
            else:
                consecutive_absent = 0
        except Exception:
            consecutive_absent = 0
        if i < attempts - 1:
            sleep(min(2 ** (i + 1), 8))
    return False


def down(
    kind: str,
    value: int | str | None,
    *,
    domain: str,
    client,
    service: str = "finance_report/app",
    attempts: int = 6,
    _sleep=None,
) -> PreviewResult:
    """Tear down the preview stack for one alias, destroying its ephemeral DB volume.

    ``service`` must match the one passed to the corresponding ``up`` (it selects the
    Dokploy project + compose-name prefix the alias was created under).

    Finds the alias's compose by its deterministic name and deletes it with
    delete_volumes=True so the throwaway `preview_db` named volume is removed too —
    nothing the preview wrote survives teardown. A missing compose (already gone) is
    not an early exit: every call reads the container inventory until two consecutive
    reads show zero containers of the slot, so teardown is idempotent and always proved.
    """
    config = preview_service_config(service)
    alias = preview_alias(kind, value, slug_prefix=config.slug_prefix)
    existing = _find_compose_for_down(client, config.project, alias)
    url = alias.app_url(domain=domain, base_subdomain=config.base_subdomain)
    compose_id = None
    if existing:
        compose_id = existing["composeId"]
        client.delete_compose(compose_id, delete_volumes=True)
    cleared = wait_for_containers_absent(
        client, service, f"-{alias.alias}", attempts=attempts, _sleep=_sleep
    )
    return PreviewResult(
        action="down",
        alias=alias.alias,
        compose_id=compose_id,
        sha=None,
        url=url,
        healthy=None,
        containers_cleared=cleared,
    )


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    """Minimal GET returning (status, body-head). 0 status on a connection error.

    Same follow-redirects/timeout discipline the retired route-canary used (#543), so preview readiness keeps the same
    stdlib-only probe; an injected getter replaces it in tests (no real HTTP).
    """
    request = Request(url, headers={"User-Agent": "infra2-preview-lifecycle/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(4096).decode(
                "utf-8", errors="replace"
            )
    except HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", errors="replace")
    except (URLError, TimeoutError) as exc:
        return 0, str(getattr(exc, "reason", exc))


def _wait_for_readiness(
    base_url: str,
    probes: tuple[PreviewReadinessProbe, ...],
    *,
    expected_versions: dict[str, str],
    timeout: int,
    interval: int,
    http_get=None,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> bool:
    """Require every configured public surface and its exact deploy version via SDK."""
    raw_getter = http_get or _http_get

    def sdk_getter(u: str) -> tuple[int, str]:
        try:
            return raw_getter(u, 10)
        except TypeError:
            return raw_getter(u)

    # Validate version sources first (fail-closed on unknown source)
    for probe in probes:
        if probe.version_fields and probe.version_source not in expected_versions:
            raise ValueError(
                f"no expected preview version for source {probe.version_source!r}"
            )

    deadline = _now() + max(0, timeout)
    while True:
        all_ready = bool(probes)
        for probe in probes:
            expected_ver = (
                expected_versions.get(probe.version_source, "")
                if probe.version_fields
                else ""
            )
            url = f"{base_url.rstrip('/')}{probe.path}"
            try:
                poll_until_healthy(
                    url,
                    http_get=sdk_getter,
                    expected_version=expected_ver,
                    version_json_keys=probe.version_fields or ("git_sha", "version"),
                    max_attempts=1,
                    interval_seconds=0,
                    sleep=lambda _: None,
                )
            except RuntimeError:
                all_ready = False
        if all_ready:
            return True
        if _now() >= deadline:
            return False
        _sleep(max(1, interval))
