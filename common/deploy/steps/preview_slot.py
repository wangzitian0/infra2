"""Dynamic multi-alias preview slot lifecycle steps for unified SyncPipeline v2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import secrets
import time
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx

from common.core.environ import infra_domain
from common.core.service_identity import ServiceIdentity
from common.deploy.app_contract import APP_SOURCES
from common.deploy.compose_compile import SOURCE_RAW, load_stack
from common.deploy.compose_lock import compose_write_lock
from common.deploy.contract import service_spec
from common.deploy.dokploy_adapter import (
    deployment_ids as _deployment_ids,
    raw_source_fields,
)
from common.deploy.env_config import (
    PREVIEW_ENVIRONMENT,
    PreviewAlias,
    PreviewReadinessProbe,
    otel_env,
    preview_alias,
    preview_service_config,
)
from common.deploy.env_util import _parse_env_text
from common.deploy.evidence import poll_until_healthy
from common.deploy.git_refs import resolve_to_sha
from common.deploy.routing import PreviewStackCoordinates, derive_preview_coordinates
from common.deploy.steps.app_rollout import wait_for_rollout
from common.observability.openpanel import openpanel_env

PreviewType = Literal["branch", "pr"]

_SOURCE_APP_COMPOSE = "app"
_VAULT_CRED_KEYS = ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID")
_AMBIGUOUS_CREATE_RECOVERY_ATTEMPTS = 4
_AMBIGUOUS_CREATE_RECOVERY_INTERVAL_SECONDS = 2


@dataclass(frozen=True)
class PreviewSlotOptions:
    """Service, git-source, and readiness flags for preview SyncPipeline."""

    service: str = "finance_report/app"
    image_ref: str | None = None
    iac_ref: str = ""
    branch: str = "main"
    wait: bool = True
    health_timeout: int = 600
    health_interval: int = 10


@dataclass(frozen=True)
class PreviewSlotDeps:
    """Injectable Dokploy, git-source, HTTP, and clock deps for preview SyncPipeline."""

    client: Any = None
    domain: str = ""
    github_id: str | None = None
    repo_owner: str = "wangzitian0"
    repo_name: str = "infra2"
    repo: str | None = None
    http_get: Any = None
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class PreviewSlotResult:
    action: str  # "up" | "down"
    alias: str
    compose_id: str | None
    sha: str | None
    url: str
    healthy: bool | None
    containers_cleared: bool | None = None


def validate_domain(domain: str) -> str:
    """Reject empty or whitespace-containing preview domains."""
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(f"invalid domain {domain!r}: non-empty, no whitespace")
    return domain


def render_throwaway_secrets(config) -> dict[str, str]:
    """Generate fresh ephemeral secrets for throwaway preview slots (zero Vault)."""
    return {name: secrets.token_urlsafe(24) for name in config.throwaway_secrets}


def source_app_vault_creds(client, project: str, source_env: str) -> dict[str, str]:
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


def preview_coordinates(
    service: str, alias: PreviewAlias, domain: str | None = None
) -> PreviewStackCoordinates:
    config = preview_service_config(service)
    return derive_preview_coordinates(
        config.base_subdomain or service.split("/")[-1],
        alias.deployment_environment,
        base_domain=domain,
    )


def build_preview_env_vars(
    alias: PreviewAlias,
    *,
    service: str,
    sha: str,
    domain: str,
    image_ref: str | None = None,
    iac_ref: str = "",
    _now=time.time,
) -> dict[str, str]:
    """Assemble Dokploy compose environment variables for one preview slot."""
    from common.deploy.deployer import load_deployer_class

    config = preview_service_config(service)
    coords = preview_coordinates(service, alias, domain=domain)
    image_tag = image_ref or sha[:7]
    config_hash = f"preview-{image_tag}-{int(_now() * 1000)}"
    env_vars = {
        "IMAGE_TAG": image_tag,
        "GIT_COMMIT_SHA": image_tag,
        "NEXT_PUBLIC_APP_URL": alias.app_url(
            domain=domain, base_subdomain=config.base_subdomain
        ),
        **otel_env(domain=infra_domain()),
        **openpanel_env("preview"),
        "TIER_CPU_SHARES": str(coords.cpu_shares),
        "ENV_SUFFIX": coords.env_suffix,
        "ENV_DOMAIN_SUFFIX": coords.env_domain_suffix,
        "ENV": alias.deployment_environment,
        "COMPOSE_PROFILES": "app",
        "TRAEFIK_ENABLE": "true",
        "INTERNAL_DOMAIN": domain,
        "CONFIGURATION_SHA256": config_hash,
    }
    deployer_cls = load_deployer_class(service)
    env_vars.update(
        deployer_cls.compose_env_overrides(
            env=alias.deployment_environment,
            domain=domain,
            env_suffix=coords.env_domain_suffix,
        )
    )
    if not config.throwaway_secrets:
        env_vars["S3_ENDPOINT"] = f"http://platform-s3-{config.secret_env}:9000"
        if service == "finance_report/app":
            env_vars.update(
                deployer_cls.compose_env_overrides(
                    env=config.secret_env,
                    domain=domain,
                    env_suffix=coords.env_domain_suffix,
                )
            )
            env_vars.update(
                {
                    "S3_PUBLIC_ENDPOINT": f"https://s3-{config.secret_env}.{domain}",
                    "PREFECT_API_URL": "",
                    "ENABLE_STORAGE_SWEEP": "false",
                }
            )
    vault_template = os.environ.get("VAULT_SECRETS_TEMPLATE", "")
    if vault_template and not config.throwaway_secrets:
        env_vars["VAULT_SECRETS_TEMPLATE"] = vault_template

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
    return client.find_compose_by_name(name, project, env_name=PREVIEW_ENVIRONMENT)


def _ensure_preview_compose(
    client,
    config: Any,
    alias: Any,
    _sleep: Callable[[float], None] = time.sleep,
    source_type: str = "github",
) -> str:
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
            source_type=source_type,
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


def _preview_source(
    config: Any, opt: PreviewSlotOptions, dep: PreviewSlotDeps
) -> tuple[str, dict[str, Any]]:
    stack = load_stack(config.compose_path, mode="preview")
    if stack.source_type == SOURCE_RAW:
        return SOURCE_RAW, raw_source_fields(stack.compiled)
    gh_id = (
        dep.github_id
        if dep.github_id is not None
        else dep.client.get_github_provider_id()
    )
    if not gh_id:
        raise RuntimeError(
            "no GitHub provider configured in Dokploy; add one in Settings -> Git Providers"
        )
    return "github", {
        "source_type": "github",
        "githubId": gh_id,
        "repository": dep.repo_name,
        "owner": dep.repo_owner,
        "branch": opt.branch,
        "composePath": config.compose_path,
        "autoDeploy": False,
    }


def check_containers_absent(
    client, service: str, slot_suffix: str
) -> tuple[bool, list[str]]:
    """Inspect Dokploy container inventory for surviving slot containers."""
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
    sleep_fn = _sleep or time.sleep
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
            sleep_fn(min(2 ** (i + 1), 8))
    return False


def _http_get(url: str, timeout: float) -> tuple[int, str]:
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
    """Require every configured public surface and its exact deploy version."""
    raw_getter = http_get or _http_get

    def sdk_getter(u: str) -> tuple[int, str]:
        try:
            return raw_getter(u, 10)
        except TypeError:
            return raw_getter(u)

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


def execute_preview_up(
    kind: str,
    value: int | str | None,
    *,
    code: str,
    options: PreviewSlotOptions | None = None,
    deps: PreviewSlotDeps | None = None,
) -> PreviewSlotResult:
    """Deploy code to a dynamic preview slot through unified SyncPipeline."""
    opt = options or PreviewSlotOptions()
    dep = deps or PreviewSlotDeps()
    validate_domain(dep.domain)
    if dep.client is None:
        raise ValueError("execute_preview_up requires deps.client")
    config = preview_service_config(opt.service)
    alias = preview_alias(kind, value, slug_prefix=config.slug_prefix)

    if dep.repo is None:
        slug = APP_SOURCES.get(opt.service)
        if not slug:
            raise ValueError(
                f"Service {opt.service!r} has no registered source repository in APP_SOURCES"
            )
        effective_repo = f"https://github.com/{slug}.git"
    else:
        effective_repo = dep.repo
    sha = resolve_to_sha(code, repo=effective_repo)

    env_vars = build_preview_env_vars(
        alias,
        service=opt.service,
        sha=sha,
        domain=dep.domain,
        image_ref=opt.image_ref,
        iac_ref=opt.iac_ref,
        _now=dep.now,
    )
    if config.throwaway_secrets:
        env_vars.update(render_throwaway_secrets(config))
    else:
        env_vars.update(
            source_app_vault_creds(dep.client, config.project, config.secret_env)
        )

    source_type, source_fields = _preview_source(config, opt, dep)
    had_existing_compose = (
        _find_compose(dep.client, config.project, alias.compose_name) is not None
    )
    compose_id = _ensure_preview_compose(
        client=dep.client,
        config=config,
        alias=alias,
        _sleep=dep.sleep,
        source_type=source_type,
    )

    healthy: bool | None = None
    url = alias.app_url(domain=dep.domain, base_subdomain=config.base_subdomain)
    expected_versions = {
        "runtime": opt.image_ref or sha[:7],
        "source": sha[:7],
    }

    with compose_write_lock(compose_id):
        before_ids = (
            _deployment_ids(dep.client.get_compose_deployments(compose_id))
            if opt.wait
            else set()
        )
        dep.client.update_compose(compose_id, **source_fields)
        dep.client.update_compose_env(compose_id, env_vars=env_vars, replace=True)

        trigger_epoch = dep.now()
        dep.client.deploy_compose(compose_id)
        if opt.wait:
            try:
                wait_for_rollout(
                    dep.client,
                    compose_id,
                    before_ids,
                    timeout=opt.health_timeout,
                    interval=opt.health_interval,
                    _sleep=dep.sleep,
                    min_started_at=trigger_epoch,
                )
            except RuntimeError as exc:
                if (
                    not had_existing_compose
                    or "deploy rollout entered error" not in str(exc)
                ):
                    raise
                dep.client.delete_compose(compose_id, delete_volumes=True)
                if callable(getattr(dep.client, "get_containers", None)):
                    wait_for_containers_absent(
                        dep.client,
                        opt.service,
                        alias.env_suffix,
                        _sleep=dep.sleep,
                    )
                compose_id = _ensure_preview_compose(
                    client=dep.client,
                    config=config,
                    alias=alias,
                    _sleep=dep.sleep,
                    source_type=source_type,
                )
                before_ids = _deployment_ids(
                    dep.client.get_compose_deployments(compose_id)
                )
                dep.client.update_compose(compose_id, **source_fields)
                dep.client.update_compose_env(
                    compose_id, env_vars=env_vars, replace=True
                )
                trigger_epoch = dep.now()
                dep.client.deploy_compose(compose_id)
                wait_for_rollout(
                    dep.client,
                    compose_id,
                    before_ids,
                    timeout=opt.health_timeout,
                    interval=opt.health_interval,
                    _sleep=dep.sleep,
                    min_started_at=trigger_epoch,
                )
            healthy = _wait_for_readiness(
                url,
                config.readiness_probes,
                expected_versions=expected_versions,
                timeout=opt.health_timeout,
                interval=opt.health_interval,
                http_get=dep.http_get,
                _sleep=dep.sleep,
                _now=dep.monotonic,
            )
            if not healthy:
                raise TimeoutError(
                    f"preview {alias.alias} did not serve the requested deployment "
                    f"identity within {opt.health_timeout}s"
                )

    return PreviewSlotResult(
        action="up",
        alias=alias.alias,
        compose_id=compose_id,
        sha=sha,
        url=url,
        healthy=healthy,
    )


def execute_preview_down(
    kind: str,
    value: int | str | None,
    *,
    domain: str,
    client,
    service: str = "finance_report/app",
    attempts: int = 6,
    _sleep=None,
) -> PreviewSlotResult:
    """Tear down a preview slot and its ephemeral DB volume, confirming container absence."""
    config = preview_service_config(service)
    alias = preview_alias(kind, value, slug_prefix=config.slug_prefix)
    existing = _find_compose(client, config.project, alias.compose_name)
    url = alias.app_url(domain=domain, base_subdomain=config.base_subdomain)
    compose_id = None
    if existing:
        compose_id = existing["composeId"]
        client.delete_compose(compose_id, delete_volumes=True)
    cleared = wait_for_containers_absent(
        client, service, f"-{alias.alias}", attempts=attempts, _sleep=_sleep
    )
    return PreviewSlotResult(
        action="down",
        alias=alias.alias,
        compose_id=compose_id,
        sha=None,
        url=url,
        healthy=None,
        containers_cleared=cleared,
    )
