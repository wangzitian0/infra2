#!/usr/bin/env python3
"""Manual lifecycle for the multi-alias PREVIEW environment.

The preview env (deploy_env_config.py) is a *family* of throwaway stacks, one per
alias — ``main`` / ``pr-<N>`` / ``commit-<sha7>`` — each its OWN Dokploy compose with
its OWN ephemeral database. This module is the importable library that creates, deploys,
health-checks, and tears those stacks down, for every service registered in
``deploy_env_config.preview_service_config`` (finance_report/app, truealpha/app — #522).
Unlike staging/prod (a fixed compose driven by libs.deploy.promote.deploy), preview is
dynamic: the compose for an alias is found-or-created here by a deterministic name, so
any number of aliases coexist and outlive a CI run until explicitly torn down.

Design seams (mirroring libs.deploy.promote / resolve_deploy_ref):
- code -> sha           : resolve_deploy_ref.resolve_to_sha (the App image tag to pull)
- service -> config     : deploy_env_config.preview_service_config (project / compose
                          path / db name / base_subdomain — the per-service knobs)
- (kind, value) -> ids  : deploy_env_config.preview_alias (suffix / url / slug / label)
- side effects          : the injected Dokploy client (create/update/deploy/delete)
- readiness             : an injected http getter polls <base_subdomain>-<alias>/api/health

Everything with side effects takes an injected client/getter so the orchestration is
unit-testable with a mock — NO live Dokploy/HTTP call happens in tests. The compose
template is pulled from infra2 via Dokploy's GitHub source (sourceType="github",
composePath=...), the same mechanism libs.deploy.deployer uses, so the relative vault-agent
files the template mounts (vault-agent.hcl / secrets.ctmpl) exist on the Dokploy host.

This is a pure importable backend — the operator surface is the deploy_v2 CLI:
    python -m tools.deploy_v2 --type preview/pr --version-ref 5 --iac-ref main --domain zitian.party
    python -m tools.deploy_v2 --type preview/pr --version-ref 5 --iac-ref main --domain zitian.party --down
``--service`` selects the target (default finance_report/app); truealpha/app previews use
    python -m tools.deploy_v2 --service truealpha/app --type preview/pr --version-ref 5 --iac-ref main --domain zitian.party
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx  # the injected Dokploy client raises httpx errors on a transient API blip

from libs.common import infra_domain
from libs.deploy_env_config import (
    PREVIEW_ENVIRONMENT,
    PreviewAlias,
    otel_env,
    preview_alias,
    preview_service_config,
)
from tools.openpanel_clients import openpanel_env
from tools.resolve_deploy_ref import resolve_to_sha


@dataclass(frozen=True)
class PreviewResult:
    action: str  # "up" | "down"
    alias: str
    compose_id: str | None
    sha: str | None
    url: str
    healthy: bool | None  # None when no health check was performed (down / --no-wait)


# The source env's app compose to read AppRole creds from (its name in Dokploy).
_SOURCE_APP_COMPOSE = "app"
# The runtime AppRole creds the preview vault-agent logs in with. Preview reads the source
# env's secret path, so it reuses that env's role verbatim — the same creds staging itself
# runs with (injected once by `invoke setup-approle`); see _source_app_vault_creds.
_VAULT_CRED_KEYS = ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID")


def _source_app_vault_creds(client, project: str, source_env: str) -> dict[str, str]:
    """Read the AppRole creds the ``source_env`` app compose runs with, in ``project``.

    Previews reuse the source env's AppRole (they read its secret path via the service's
    ``PreviewServiceConfig.secret_env``), exactly the creds staging runs with — set once on
    the source compose by ``invoke setup-approle``. We copy them onto the preview compose at
    deploy time because a throwaway alias is recreated each ``up`` (and deleted on ``down``),
    so it can never rely on a manual one-time injection persisting the way a fixed env does.
    """
    comp = client.find_compose_by_name(_SOURCE_APP_COMPOSE, project, env_name=source_env)
    if not comp:
        raise RuntimeError(
            f"cannot source preview Vault creds: no {_SOURCE_APP_COMPOSE!r} compose in "
            f"{project}/{source_env}"
        )
    env = client.get_compose_env(comp["composeId"]) or ""
    creds = {}
    for line in env.splitlines():
        key, _, value = line.partition("=")
        if key in _VAULT_CRED_KEYS and value:
            creds[key] = value
    missing = [k for k in _VAULT_CRED_KEYS if k not in creds]
    if missing:
        raise RuntimeError(
            f"{source_env} {_SOURCE_APP_COMPOSE!r} compose is missing Vault creds "
            f"{missing}; run `invoke setup-approle --project {project}` first"
        )
    return creds


def _validate_domain(domain: str) -> str:
    """Reject a malformed domain (empty or containing whitespace).

    Whitespace in the domain would corrupt the Dokploy host (cloud.<domain>) and the
    Traefik Host() rules / env blob, so it is rejected up front for BOTH up and down.
    Returns the validated domain unchanged so callers can use the result inline.
    """
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(f"invalid domain {domain!r}: non-empty, no whitespace")
    return domain


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
    """Assemble the Dokploy compose env for one preview alias.

    Mirrors libs.deploy.promote.deploy's shared keys (IMAGE_TAG/GIT_COMMIT_SHA short-sha,
    NEXT_PUBLIC_APP_URL, ENV_SUFFIX/ENV_DOMAIN_SUFFIX, COMPOSE_PROFILES, TRAEFIK_ENABLE,
    INTERNAL_DOMAIN, IAC_CONFIG_HASH cache-bust) and adds the preview-only bits: ENV is
    the alias display label (telemetry consumes it), PREVIEW_SECRET_ENV picks which env's
    Vault app secrets to render, and the ephemeral-DB knobs point DATABASE_URL at the
    stack's own local postgres.

    NOTE: VAULT_ADDR / VAULT_ROLE_ID / VAULT_SECRET_ID are NOT set here — ``up`` injects
    them separately via ``_source_app_vault_creds`` (read from the source env's app compose
    each deploy, since a throwaway alias can't rely on a one-time injection persisting).
    Keeping them out of this pure assembler means it never logs or embeds the creds.
    """
    config = preview_service_config(service)
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
        # a per-alias project). staging/prod get this via libs.deploy.promote; preview was
        # the missing path — without it OPENPANEL_CLIENT_ID stayed empty and analytics
        # silently no-op'd on every preview.
        **openpanel_env("preview"),
        "ENV_SUFFIX": alias.env_suffix,
        "ENV_DOMAIN_SUFFIX": alias.domain_suffix,
        # ENV is the alias display label so the telemetry identity contract
        # (core.environments §4.5: deployment.environment = pr-<N>/commit-<sha>/main)
        # is carried into the running stack.
        "ENV": alias.deployment_environment,
        # Vault app-secrets source env (preview has no per-alias Vault path).
        "PREVIEW_SECRET_ENV": config.secret_env,
        "COMPOSE_PROFILES": "app",
        "TRAEFIK_ENABLE": "true",
        "INTERNAL_DOMAIN": domain,
        "IAC_CONFIG_HASH": config_hash,
        # Ephemeral DB knobs — the compose template builds PREVIEW_DATABASE_URL from
        # these and overrides DATABASE_URL to the local `db` service (never the shared
        # staging/prod postgres for this service). Per-alias name keeps the DSN unambiguous
        # in logs.
        "PREVIEW_DB_USER": "preview",
        "PREVIEW_DB_PASSWORD": "preview",
        "PREVIEW_DB_NAME": config.db_name,
    }
    from libs.deploy_contract import service_spec
    from libs.service_identity import ServiceIdentity

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
    """Stand up (or update) the preview stack for one alias and deploy ``code``.

    ``service`` (default ``finance_report/app``, the original preview-capable service)
    selects the :func:`~libs.deploy_env_config.preview_service_config` — which Dokploy
    project, compose template, and ephemeral-DB name this alias uses.

    Resolves code->sha, computes the alias identity, finds-or-creates this alias's own
    Dokploy compose (GitHub-sourced preview template), pushes the env, triggers a deploy,
    and — when wait=True — polls the alias's ``/api/health`` until 200. Idempotent:
    re-running an alias updates and redeploys the same compose in place.
    """
    _validate_domain(domain)
    config = preview_service_config(service)

    alias = preview_alias(kind, value, slug_prefix=config.slug_prefix)
    sha = resolve_to_sha(code, repo=repo) if repo is not None else resolve_to_sha(code)
    env_vars = _preview_env_vars(
        alias,
        service=service,
        sha=sha,
        domain=domain,
        image_ref=image_ref,
        iac_ref=iac_ref,
        _now=_now,
    )
    # Inject the runtime AppRole creds the preview vault-agent logs in with — the same
    # role the source env runs with. Without them vault-agent crash-loops and the app
    # never becomes healthy. Merged before the compose env is pushed below.
    env_vars.update(
        _source_app_vault_creds(client, config.project, config.secret_env)
    )

    # Find-or-create THIS alias's compose by its deterministic name. The GitHub source
    # fields make Dokploy pull the preview compose template (+ its mounted vault files)
    # from infra2; autoDeploy=False so only this manual lifecycle triggers a deploy.
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

    existing = _find_compose(client, config.project, alias.compose_name)
    if not existing:
        # Self-provision the preview environment (idempotent). The fixed envs
        # (staging/prod) are pre-provisioned by convention, but preview is DYNAMIC — its
        # composes are created and destroyed per alias — so the lifecycle owns its parent
        # environment too. This makes a fresh box / first-ever preview reproducible with
        # no out-of-band `invoke dokploy_env.env-ensure` step (the missing-env gap that
        # blocked the first live canary). config.project/PREVIEW_ENVIRONMENT are resolved
        # once above, so there is no typo risk in creating-if-absent.
        try:
            env_obj, _ = client.ensure_environment(config.project, PREVIEW_ENVIRONMENT)
        except ValueError as exc:
            # ensure_environment raises ValueError when the PROJECT itself is absent —
            # re-raise as a consistent fail-closed RuntimeError instead of leaking it.
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
        # Teardown-convergence invariant (#921 / D8): the stable key is the compose
        # *record name* (alias.compose_name). `up` creates the record under it and `down`
        # finds by it, then tears down via `delete_compose(composeId)`. Dokploy assigns the
        # docker appName as name + a random suffix (so name != appName in reality), but
        # teardown routes through the composeId, so it prunes the right project regardless —
        # it never keys off a bare docker project name, which is exactly the divergence that
        # leaked orphans in infra2#310. See libs/tests/test_preview_teardown_convergence.py.
        # Create as github source from the start (Dokploy accepts this and keeps
        # sourceType) so the compose is never momentarily a raw compose with an empty
        # composeFile. The github *binding* (githubId/owner/repository/branch/composePath)
        # still has to be re-applied below — compose.create drops everything but sourceType.
        created = client.create_compose(
            environment_id=environment_id,
            name=alias.compose_name,
            app_name=alias.compose_name,
            source_type="github",
        )
        compose_id = created["composeId"]
    else:
        compose_id = existing["composeId"]

    # Configure source + env on BOTH paths. Dokploy's compose.create persists ONLY the
    # basic fields — it silently drops the github source (githubId/owner/repository/
    # branch/composePath) and the env blob — so a first-ever preview deploy would land
    # source-less and env-less and fail at deploy time ("Github Provider not found" / all
    # compose vars blank → IMAGE_TAG defaults to :latest). The github source + env only
    # stick via these follow-up compose.update / compose.update-env calls, which also
    # re-assert them on a redeploy. MERGE the env so runtime AppRole creds
    # (VAULT_ROLE_ID / VAULT_SECRET_ID / VAULT_ADDR) injected at setup survive a redeploy.
    client.update_compose(compose_id, **source_fields)
    client.update_compose_env(compose_id, env_vars=env_vars)

    client.deploy_compose(compose_id)

    healthy: bool | None = None
    url = alias.app_url(domain=domain, base_subdomain=config.base_subdomain)
    if wait:

        def _deploy_status():
            # Best-effort: a transient API blip returns None (keep waiting on HTTP); only a
            # definitive "error" status short-circuits the wait.
            try:
                return (client.get_compose(compose_id) or {}).get("composeStatus")
            except httpx.HTTPError:
                return None

        healthy = _wait_for_health(
            f"{url}/api/health",
            timeout=health_timeout,
            interval=health_interval,
            http_get=http_get,
            deploy_status=_deploy_status,
            _sleep=_sleep,
            _now=_monotonic,
        )
        if not healthy:
            raise TimeoutError(
                f"preview {alias.alias} did not become healthy at {url}/api/health "
                f"within {health_timeout}s"
            )

    return PreviewResult(
        action="up",
        alias=alias.alias,
        compose_id=compose_id,
        sha=sha,
        url=url,
        healthy=healthy,
    )


def down(
    kind: str,
    value: int | str | None,
    *,
    domain: str,
    client,
    service: str = "finance_report/app",
) -> PreviewResult:
    """Tear down the preview stack for one alias, destroying its ephemeral DB volume.

    ``service`` must match the one passed to the corresponding ``up`` (it selects the
    Dokploy project + compose-name prefix the alias was created under).

    Finds the alias's compose by its deterministic name and deletes it with
    delete_volumes=True so the throwaway `preview_db` named volume is removed too —
    nothing the preview wrote survives teardown. A no-op (compose_id=None) if the
    alias was already gone, so teardown is safely idempotent.
    """
    config = preview_service_config(service)
    alias = preview_alias(kind, value, slug_prefix=config.slug_prefix)
    existing = _find_compose(client, config.project, alias.compose_name)
    url = alias.app_url(domain=domain, base_subdomain=config.base_subdomain)
    if not existing:
        return PreviewResult(
            action="down",
            alias=alias.alias,
            compose_id=None,
            sha=None,
            url=url,
            healthy=None,
        )
    compose_id = existing["composeId"]
    client.delete_compose(compose_id, delete_volumes=True)
    return PreviewResult(
        action="down",
        alias=alias.alias,
        compose_id=compose_id,
        sha=None,
        url=url,
        healthy=None,
    )


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    """Minimal GET returning (status, body-head). 0 status on a connection error.

    Same follow-redirects/timeout discipline the retired route-canary used (#543), so preview readiness keeps the same
    stdlib-only probe; an injected getter replaces it in tests (no real HTTP).
    """
    request = Request(url, headers={"User-Agent": "infra2-preview-lifecycle/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(256).decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read(256).decode("utf-8", errors="replace")
    except (URLError, TimeoutError) as exc:
        return 0, str(getattr(exc, "reason", exc))


def _wait_for_health(
    health_url: str,
    *,
    timeout: int,
    interval: int,
    http_get=None,
    deploy_status=None,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> bool:
    """Poll ``health_url`` until it returns HTTP 200, or the deadline passes.

    Returns True on the first 200, False if the window elapses first. If ``deploy_status``
    is given and reports ``"error"``, raise immediately — the deploy itself failed (e.g. an
    unpublished image or a build error), so the stack can NEVER become healthy and waiting
    out the full timeout only hides the real reason. Side-effect free apart from the
    injected getter/status, so tests drive it with a fake clock + fake getter.
    """
    getter = http_get or _http_get
    deadline = _now() + max(0, timeout)
    while True:
        if deploy_status is not None and deploy_status() == "error":
            raise RuntimeError(
                f"deploy failed (Dokploy composeStatus=error) before {health_url} became "
                "healthy — check the Dokploy deploy log (image not published / build error?)"
            )
        status, _ = getter(health_url, 10)
        if status == 200:
            return True
        if _now() >= deadline:
            return False
        _sleep(max(1, interval))
