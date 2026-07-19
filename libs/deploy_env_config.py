#!/usr/bin/env python3
"""Per-environment deploy config — the env REGIME each deploy_v2 ``type`` derives.

deploy_v2's coordinate is ``(service, type, version_ref, iac_ref)``; ``env`` is NOT an
input axis — it is derived from ``type`` (e.g. ``prod`` -> env ``prod``, ``preview/*`` ->
env ``preview``). This module owns that derived env regime and what it implies:

    type     -> deploy_contract.DEPLOY_TYPES (which env a type maps to)
    env      -> THIS module                  (which compose, URL, suffix, data default, gates)
    data_lane -> EnvConfig.data_default       (derived; not a deploy_v2 input axis)

This module owns the env regime: each deploy environment maps to the Dokploy compose
it targets, its public URL pattern, the container/domain suffix, the default data
source, and prod gating. Single source so the deploy primitive, docs, and the contract
test all derive from here instead of re-stating per-env values across workflows.

The staging and production compose ids are mirrored from the App-repo deploy.yml
workflow and become the sole copy once P2 step 5 removes them there.
No deploy is performed here — like the resolver, this is pure, importable config.

Compose_id drift recovery (#524): every ``compose_id`` literal below (in ``_ENVIRONMENTS``
and ``_APP_COMPOSE_OVERRIDES``) is read ONCE from the live Dokploy API and never
re-verified — if the underlying Dokploy compose is ever deleted and recreated, it gets a
fresh ``composeId`` and the literal here goes stale. ``tools/app_compose_id_drift.py`` is a
scheduled (not PR-blocking) CI check that re-resolves each entry via
``DokployClient.find_compose_by_name`` and fails loud if the live ``composeId`` no longer
matches. If it fails: re-run ``find_compose_by_name(compose_name, project_name, env_name)``
for the affected entry (``bespoke_app_compose_targets()`` below names the exact
project/env/compose-name to use) and update the hardcoded literal to the returned
``composeId``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from libs.common import normalize_env_name

# data default per env. Non-prod defaults to `staging` data (operator choice); prod is
# always real prod data. A PR sha never runs on prod data and prod data never leaves
# prod un-anonymized (the G2 / RL-DATA red lines, finance_report#877). deploy_v2 derives
# data_lane from this value and enforces the red lines before any side effect.
_DATA_STAGING = "staging"
_DATA_PROD = "prod"


@dataclass(frozen=True)
class EnvConfig:
    name: str
    compose_id: str | None  # None = dynamic per-PR (preview, looked up by lifecycle)
    env_suffix: str
    app_url_pattern: str  # contains {domain}, and {number} for the dynamic preview env
    data_default: str
    gates_prod: bool = False  # prod must deploy here first (staging)
    requires_staging_first: bool = False  # this env (prod) requires a staging deploy
    dynamic: bool = False  # per-PR; compose_id/suffix are resolved at deploy time

    def app_url(self, *, domain: str, number: int | str | None = None) -> str:
        """Concrete public URL: fill {domain} (always) and {number} (preview only)."""
        return self.app_url_pattern.format(
            domain=domain, number="" if number is None else number
        )


_ENVIRONMENTS: dict[str, EnvConfig] = {
    "staging": EnvConfig(
        name="staging",
        compose_id="A6V-hbJlgHMwgPDoTDnhH",
        env_suffix="-staging",
        app_url_pattern="https://report-staging.{domain}",
        data_default=_DATA_STAGING,
        gates_prod=True,
    ),
    "prod": EnvConfig(
        name="prod",
        compose_id="lNn9gVS1Zyw79Jzw5dlbu",
        env_suffix="",
        app_url_pattern="https://report.{domain}",
        data_default=_DATA_PROD,
        requires_staging_first=True,
    ),
    "preview": EnvConfig(
        name="preview",
        compose_id=None,  # per-PR compose, created/looked up by the preview lifecycle
        env_suffix="",  # actually -pr-<number>, set per-PR at deploy time
        app_url_pattern="https://report-pr-{number}.{domain}",
        data_default=_DATA_STAGING,
        dynamic=True,
    ),
}

ENVIRONMENTS = tuple(_ENVIRONMENTS)


def env_config(env: str) -> EnvConfig:
    """Return the EnvConfig for a deploy env. Raises ValueError for an unknown env."""
    try:
        return _ENVIRONMENTS[env]
    except KeyError:
        raise ValueError(
            f"unknown deploy env {env!r}: expected one of {sorted(_ENVIRONMENTS)}"
        ) from None


def for_env_suffix(env: str, *, number: int | str | None = None) -> str:
    """The container/domain ENV_SUFFIX for a deploy env (preview is -pr-<number>)."""
    cfg = env_config(env)
    if cfg.dynamic and number is not None:
        return f"-pr-{number}"
    return cfg.env_suffix


def with_compose_id(env: str, compose_id: str) -> EnvConfig:
    """Bind a runtime compose_id (used for the dynamic preview env)."""
    return replace(env_config(env), compose_id=compose_id)


# ---------------------------------------------------------------------------
# Per-app fixed-compose targets (#500)
# ---------------------------------------------------------------------------
#
# `env_config(env)` above owns the env REGIME (suffix/data/gates) — platform-wide policy,
# identical for every fixed-compose app. `compose_id` and `app_url_pattern` are the two
# facts that genuinely vary PER APP (which Dokploy compose, which public URL). This
# overlay is the generalization point: finance_report keeps calling `env_config(env)`
# directly (byte-identical, no change), and other bespoke apps register their overrides
# here instead of duplicating the whole EnvConfig table.


@dataclass(frozen=True)
class _ComposeOverride:
    compose_id: str | None
    app_url_pattern: str  # contains {domain}


# truealpha/app: version-pinned staging promotion (#500), generalizing the
# finance_report fixed-compose path. compose_id verified live against the Dokploy API
# 2026-07-18 (project "truealpha", env "staging", compose "app" -> w4zo_fm9d2PnUY8ULzNO7).
# prod compose_id is None — the truealpha Dokploy project's `production` environment has
# no compose yet, and truealpha's sender (truealpha#333) blocks PRODUCTION requests until
# infra2 evidence support lands; a None compose_id fails closed if that ever changes
# without this being updated first.
_APP_COMPOSE_OVERRIDES: dict[str, dict[str, _ComposeOverride]] = {
    "truealpha/app": {
        "staging": _ComposeOverride(
            "w4zo_fm9d2PnUY8ULzNO7", "https://truealpha-staging.{domain}"
        ),
        "prod": _ComposeOverride(None, "https://truealpha.{domain}"),
    },
}


def app_compose_env_config(service: str, env: str) -> EnvConfig:
    """The per-service EnvConfig for a fixed-compose app deploy (#500).

    finance_report/app: identical to ``env_config(env)`` (no override registered).
    Other bespoke apps: the env regime from ``env_config(env)`` with ``compose_id`` /
    ``app_url_pattern`` overlaid from ``_APP_COMPOSE_OVERRIDES``. Raises ValueError for
    a service/env with no registered override (e.g. truealpha/app + preview — #500 does
    not wire preview for it, it has no preview compose stack yet).
    """
    base = env_config(env)
    overrides_for_service = _APP_COMPOSE_OVERRIDES.get(service)
    if overrides_for_service is None:
        return base
    override = overrides_for_service.get(env)
    if override is None:
        raise ValueError(
            f"no compose target registered for service {service!r} env {env!r}"
        )
    return replace(
        base, compose_id=override.compose_id, app_url_pattern=override.app_url_pattern
    )


def services_without_prod_compose() -> frozenset[str]:
    """Bespoke apps whose registered prod compose_id is None — i.e. NO production
    deployment exists yet (#542). The None literal above IS the declaration
    ("fails closed if prod ever appears without this being updated first"), so
    "not yet in production" is DERIVED from it rather than declared a second
    time — consumed by the vault self-refresh audit's production exclusion
    (libs.vault_self_refresh_audit.inventory_ids_not_in_production).
    """
    return frozenset(
        service
        for service, overrides in _APP_COMPOSE_OVERRIDES.items()
        if "prod" in overrides and overrides["prod"].compose_id is None
    )


# The baseline bespoke app: finance_report/app has no _APP_COMPOSE_OVERRIDES entry, its
# compose_id/app_url_pattern come straight from `_ENVIRONMENTS` (env_config()). It still
# needs a Dokploy project/compose-name pair for live drift verification below, so it is
# named here once rather than re-derived ad hoc.
_BASELINE_APP_SERVICE = "finance_report/app"


@dataclass(frozen=True)
class ComposeTarget:
    """One hardcoded compose_id literal this repo carries, and where to re-verify it live.

    ``project_name``/``compose_name`` are the Dokploy identity ``DokployClient.
    find_compose_by_name(compose_name, project_name, env_name=dokploy_env_name)`` needs to
    re-resolve the compose independently of the (possibly stale) hardcoded ``compose_id`` —
    see ``bespoke_app_compose_targets()``.
    """

    service: str  # e.g. "finance_report/app", "truealpha/app"
    env: str  # deploy env key, e.g. "staging", "prod"
    project_name: str  # Dokploy project name
    compose_name: str  # Dokploy compose display name
    dokploy_env_name: str  # Dokploy environment name (normalize_env_name'd)
    compose_id: str  # the literal hardcoded in this module, to verify against live


def bespoke_app_compose_targets() -> tuple[ComposeTarget, ...]:
    """Every hardcoded bespoke-app compose_id, paired with where to re-verify it live (#524).

    Single source for ``tools/app_compose_id_drift.py``: for each (service, env) with a
    registered, non-None ``compose_id`` — finance_report/app's two envs from
    ``_ENVIRONMENTS`` (it has no override) plus every ``_APP_COMPOSE_OVERRIDES`` entry —
    derive the Dokploy project name and compose display name from the service key
    (``"<project>/<compose-name>"``, e.g. ``truealpha/app`` -> project ``truealpha``,
    compose ``app``) so a caller can re-run ``find_compose_by_name`` and compare.
    """
    targets: list[ComposeTarget] = []
    project_name, compose_name = _BASELINE_APP_SERVICE.split("/", 1)
    for env_name, cfg in _ENVIRONMENTS.items():
        if cfg.dynamic or cfg.compose_id is None:
            continue  # preview is per-PR/dynamic — nothing fixed to verify here
        targets.append(
            ComposeTarget(
                service=_BASELINE_APP_SERVICE,
                env=env_name,
                project_name=project_name,
                compose_name=compose_name,
                dokploy_env_name=normalize_env_name(env_name),
                compose_id=cfg.compose_id,
            )
        )
    for service, overrides in _APP_COMPOSE_OVERRIDES.items():
        project_name, compose_name = service.split("/", 1)
        for env_name, override in overrides.items():
            if override.compose_id is None:
                continue  # not registered yet (e.g. truealpha/app prod) — nothing to verify
            targets.append(
                ComposeTarget(
                    service=service,
                    env=env_name,
                    project_name=project_name,
                    compose_name=compose_name,
                    dokploy_env_name=normalize_env_name(env_name),
                    compose_id=override.compose_id,
                )
            )
    return tuple(targets)


# ---------------------------------------------------------------------------
# Preview alias model (multi-alias, manually-deployed PREVIEW environment)
# ---------------------------------------------------------------------------
#
# The `preview` env above is a single *taxonomy* row: it says "preview is a
# dynamic, per-alias env on staging-default data". This section owns the next
# level down — the deterministic mapping from a concrete preview *alias*
# (kind, value) to the names/suffixes/URLs a single Dokploy compose stack uses.
#
# Four alias kinds coexist, each its OWN compose stack + OWN ephemeral DB. Every alias is
# uniformly <kind>-<value> (no bare special case), so downstream parses them the same way:
#   branch-<name>   -> <base>-branch-<name>.<domain> (a branch tip, default main, on demand)
#   pr-<N>          -> <base>-pr-<N>.<domain>       (a specific PR)
#   commit-<sha7>   -> <base>-commit-<sha7>.<domain> (a pinned commit)
#   tag-<v1-2-3>    -> <base>-tag-<v1-2-3>.<domain>  (a release tag, DNS-safe slug)
# where <base> is the deploying service's ``base_subdomain`` (``report`` for
# finance_report/app, ``truealpha`` for truealpha/app — libs.deploy_contract.ServiceSpec).
#
# This is PURE config: no Dokploy calls, no resolution. libs/deploy/preview.py
# turns the slug/suffix/url here into actual create_compose/deploy calls, and
# resolve_deploy_ref.py turns `code` into the sha that a `commit` alias pins.
# The `deployment_environment` label mirrors the telemetry identity contract in
# docs/ssot/core.environments.md §4.5 (values branch-<name> / pr-<N> / commit-<sha7> / tag-<slug>).

# A Dokploy compose appName / container suffix must be DNS/label-safe. PR numbers
# and 7-char shas already are; we still validate so a bad alias fails fast (a bad
# suffix would silently produce an unroutable Host() rule or a name collision).
_PR_VALUE_RE = re.compile(r"\A[1-9][0-9]*\Z")  # a positive PR number, no leading zero
_SHA7_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")  # a (lowercased) commit sha, >=7 hex
_TAG_VALUE_RE = re.compile(r"\Av\d+\.\d+\.\d+\Z")  # a release tag vX.Y.Z
_BRANCH_VALUE_RE = re.compile(r"\A[A-Za-z0-9._/-]+\Z")  # a git branch name (e.g. main)

# Every preview kind is <kind>-<value>; there is no bare special case, so any downstream
# (telemetry label, URL, compose name) parses a slot the same way. `branch` (default main)
# replaces the old bare `main` so the main-tip preview is report-branch-main.
PREVIEW_KINDS = ("branch", "pr", "commit", "tag")

# The Dokploy ENVIRONMENT name every service's preview stacks live under (kept distinct
# from staging/prod composes; the lifecycle find-or-creates this environment). Shared
# across services — each service's preview composes live in ITS OWN Dokploy PROJECT
# (see PreviewServiceConfig.project below), so a shared environment name never collides.
PREVIEW_ENVIRONMENT = "preview"


@dataclass(frozen=True)
class PreviewServiceConfig:
    """Per-service knobs the preview lifecycle (``libs.deploy.preview``) needs.

    Generalizes what was, pre-#522, a set of module-level constants hardwired to
    finance_report/app — the ONLY previously-supported service. Each registered service
    gets its own Dokploy project (composes never collide across services), compose
    slug prefix, preview compose template path, and ephemeral-DB name. ``base_subdomain``
    must match the service's ``libs.deploy_contract.ServiceSpec.base_subdomain`` (the
    preview URL is ``https://<base_subdomain>-<alias>.<domain>``); duplicated here rather
    than imported to avoid a preview<->contract import cycle (deploy_contract already
    imports this module for the fixed-env regime).
    """

    project: str  # Dokploy project name (e.g. "finance_report", "truealpha")
    slug_prefix: str  # compose display-name / appName prefix, e.g. "finance-report-preview"
    compose_path: str  # the preview compose template infra2 path, github-sourced by Dokploy
    db_name: str  # default PREVIEW_DB_NAME (the ephemeral postgres database name)
    base_subdomain: str  # e.g. "report" / "truealpha" — must match ServiceSpec.base_subdomain
    secret_env: str = (
        "staging"  # which env's Vault app-secrets path a preview borrows (no per-alias path)
    )


# finance_report/app is the original (and, pre-#522, only) preview-capable service; its
# values are byte-identical to the former module-level constants, so registering it here
# is a pure refactor — no behavior change for its existing previews/canary.
_PREVIEW_SERVICE_CONFIGS: dict[str, PreviewServiceConfig] = {
    "finance_report/app": PreviewServiceConfig(
        project="finance_report",
        slug_prefix="finance-report-preview",
        compose_path="finance_report/finance_report/preview/compose.yaml",
        db_name="finance_report",
        base_subdomain="report",
    ),
    # #522: truealpha/app preview — own Dokploy project, own ephemeral DB (never the
    # shared staging/prod truealpha-postgres). See truealpha/truealpha/preview/compose.yaml.
    "truealpha/app": PreviewServiceConfig(
        project="truealpha",
        slug_prefix="truealpha-preview",
        compose_path="truealpha/truealpha/preview/compose.yaml",
        db_name="truealpha",
        base_subdomain="truealpha",
    ),
}


def preview_service_config(service: str) -> PreviewServiceConfig:
    """Return the :class:`PreviewServiceConfig` for a preview-capable service.

    Raises ``ValueError`` for a service with no registered preview config — the same
    fail-closed shape as ``app_compose_env_config`` for the fixed-env overlay. Callers
    (``libs.deploy.preview``) should gate on ``ServiceSpec.supports_preview`` first so this
    is a defensive re-check, not the primary guard.
    """
    config = _PREVIEW_SERVICE_CONFIGS.get(service)
    if config is None:
        raise ValueError(
            f"no preview config registered for service {service!r}: expected one of "
            f"{sorted(_PREVIEW_SERVICE_CONFIGS)}"
        )
    return config


@dataclass(frozen=True)
class PreviewAlias:
    """A single preview stack's deterministic identity.

    kind/value are the human surface (``pr`` / ``5``); everything else is derived
    once, here, so the lifecycle, the compose env, the Traefik Host() rule, and the
    telemetry label all agree by construction instead of being re-formatted per call.
    """

    kind: str
    value: str
    alias: str  # the canonical alias token: main | pr-<N> | commit-<sha7>
    env_suffix: (
        str  # ENV_SUFFIX / ENV_DOMAIN_SUFFIX, e.g. -main / -pr-5 / -commit-1ab32d5
    )
    domain_suffix: str  # same value; named separately to mirror the two compose vars
    compose_name: str  # Dokploy compose display name + appName slug
    deployment_environment: str  # telemetry display value (core.environments §4.5)

    def app_url(self, *, domain: str, base_subdomain: str = "report") -> str:
        """Concrete public URL for this alias: https://<base_subdomain><suffix>.<domain>.

        ``base_subdomain`` defaults to ``"report"`` (finance_report/app, the original
        preview-capable service) so every pre-#522 call site is unchanged; a caller
        deploying a different service passes its ``ServiceSpec.base_subdomain`` /
        ``PreviewServiceConfig.base_subdomain`` explicitly (see ``libs.deploy.preview``).
        """
        return f"https://{base_subdomain}{self.env_suffix}.{domain}"


def _normalize_alias(kind: str, value: int | str | None) -> tuple[str, str]:
    """Validate (kind, value) and return the (kind, canonical-value) pair.

    `branch` takes a branch name (default `main`). `pr` requires a positive integer.
    `commit` requires a hex sha (>=7 chars) and is truncated to the 7-char short form
    used everywhere else (image tag, telemetry service.version). Raises ValueError on bad
    input — an invalid alias must fail loudly, never silently produce an unroutable stack.
    """
    if kind == "branch":
        text = (str(value).strip() if value is not None else "") or "main"
        if not _BRANCH_VALUE_RE.match(text):
            raise ValueError(f"preview branch alias needs a branch name, got {value!r}")
        return "branch", text
    if kind == "pr":
        text = str(value).strip()
        if not _PR_VALUE_RE.match(text):
            raise ValueError(
                f"preview pr alias needs a positive PR number, got {value!r}"
            )
        return "pr", text
    if kind == "commit":
        text = str(value).strip().lower()
        if not _SHA7_RE.match(text):
            raise ValueError(
                f"preview commit alias needs a hex commit sha (>=7 chars), got {value!r}"
            )
        return "commit", text[:7]  # short sha — matches IMAGE_TAG / service.version
    if kind == "tag":
        text = str(value).strip()
        if not _TAG_VALUE_RE.match(text):
            raise ValueError(
                f"preview tag alias needs a vX.Y.Z release tag, got {value!r}"
            )
        return (
            "tag",
            text,
        )  # canonical (keeps dots, for the image ref); slug uses dashes
    raise ValueError(
        f"unknown preview kind {kind!r}: expected one of {list(PREVIEW_KINDS)}"
    )


_DEFAULT_PREVIEW_SLUG_PREFIX = _PREVIEW_SERVICE_CONFIGS["finance_report/app"].slug_prefix


def preview_alias(
    kind: str,
    value: int | str | None = None,
    *,
    slug_prefix: str = _DEFAULT_PREVIEW_SLUG_PREFIX,
) -> PreviewAlias:
    """Map a preview (kind, value) to its full deterministic identity.

    Pure and total over the validated surface; the single source the lifecycle and
    the tests both derive from. Every alias is uniformly ``<kind>-<slug>``. Examples:

        preview_alias("branch", "main") -> alias branch-main,     suffix -branch-main
        preview_alias("pr", 5)          -> alias pr-5,            suffix -pr-5
        preview_alias("commit", sha)    -> alias commit-1ab32d5,  suffix -commit-1ab32d5
        preview_alias("tag", "v1.2.3")  -> alias tag-v1-2-3,      suffix -tag-v1-2-3

    ``slug_prefix`` defaults to finance_report/app's (the original preview-capable
    service) so every pre-#522 call site is unchanged; a caller deploying a different
    service passes its ``PreviewServiceConfig.slug_prefix`` explicitly.
    """
    norm_kind, norm_value = _normalize_alias(kind, value)
    # The URL/compose slug must be a single DNS label: a tag's/branch's dots and slashes
    # become dashes (tag-v1-2-3, branch-main). norm_value keeps the canonical value
    # (v1.2.3 / main) for the image ref + telemetry.
    if norm_kind in ("tag", "branch"):
        slug = norm_value.lower().replace(".", "-").replace("/", "-")
    else:
        slug = norm_value
    alias = f"{norm_kind}-{slug}"
    suffix = f"-{alias}"
    return PreviewAlias(
        kind=norm_kind,
        value=norm_value,
        alias=alias,
        env_suffix=suffix,
        domain_suffix=suffix,
        compose_name=f"{slug_prefix}-{alias}",
        deployment_environment=alias,
    )


# ---------------------------------------------------------------------------
# OTLP-collector CORS allowed-origins — derived from the FE origins (#368)
# ---------------------------------------------------------------------------
#
# The browser frontend POSTs OTLP traces to the public ingest domain
# (otel.<domain>), so the collector must echo CORS for exactly the FE origins.
# That allow-list used to be a hand-maintained literal in
# platform/11.signoz/otel-collector-config.yaml — which can silently drift from
# the FE domains it is supposed to mirror. It is now DERIVED here from the same
# env URL patterns + preview alias convention the deploy paths already use, and
# the collector config is rendered from this single source at deploy time.
#
# Local browser dev origin (the Next.js dev server). Not an infra-deployed env,
# so it is named here rather than synthesized from an EnvConfig.
_LOCAL_DEV_ORIGIN = "http://localhost:3000"

# Preview kinds that get a *wildcard* CORS origin (report-<kind>-*.<domain>).
# A wildcard is required because the concrete value (PR number / commit sha) is
# only known per-deploy. `branch-main` is enumerated explicitly below (the only
# branch alias with a public, CORS-eligible URL today) rather than wildcarded.
_PREVIEW_CORS_WILDCARD_KINDS = ("pr", "commit")
# Branch aliases with a public, CORS-eligible preview URL (currently just main).
_PREVIEW_CORS_BRANCHES = ("main",)


def cors_allowed_origins(*, domain: str) -> list[str]:
    """The OTLP collector's CORS allow-list, derived from the FE origins.

    Single source: fixed envs (staging/prod) come from their ``app_url_pattern``;
    preview aliases contribute their wildcard / branch origins; plus the local dev
    origin. Order is stable (fixed envs, then branch, then wildcard kinds, then
    local) so the rendered config is deterministic. This mirrors exactly the FE
    domains so the allow-list can never drift from them.
    """
    origins: list[str] = []
    # Fixed, non-dynamic envs (prod first, then staging) — straight from their URL pattern.
    for env_name in ("prod", "staging"):
        cfg = _ENVIRONMENTS[env_name]
        origins.append(cfg.app_url(domain=domain))
    # Preview branch aliases — a concrete public URL each. The branch-tip alias is
    # `branch-<name>` => report-branch-<name>.<domain> (matches PreviewAlias.app_url),
    # e.g. report-branch-main, NOT report-main.
    for branch in _PREVIEW_CORS_BRANCHES:
        origins.append(f"https://report-branch-{branch}.{domain}")
    # Preview pr/commit aliases — wildcard, since the value is per-deploy.
    for kind in _PREVIEW_CORS_WILDCARD_KINDS:
        origins.append(f"https://report-{kind}-*.{domain}")
    origins.append(_LOCAL_DEV_ORIGIN)
    return origins


def otel_ingest_endpoint(*, domain: str) -> str:
    """The public browser-OTLP traces endpoint for FE compose env (#368).

    Delegates to libs.common.otel_ingest_endpoint (the single construction
    point SigNoz's deploy.py also uses), so the FE endpoint and the ingest
    domain SigNoz registers can never disagree. The ingest domain is shared
    across envs (no env suffix) — only the base domain varies.
    """
    from libs.common import otel_ingest_endpoint as _build

    return _build({"INTERNAL_DOMAIN": domain})


def otel_env(*, domain: str) -> dict[str, str]:
    """The OTLP compose-env contribution injected by every FE deploy path (#368).

    Single source for ``NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT`` so the two compose
    files consume the value instead of each re-constructing the URL inline.
    """
    return {
        "NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT": otel_ingest_endpoint(domain=domain)
    }
