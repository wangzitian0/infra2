#!/usr/bin/env python3
"""Per-environment deploy config — the env REGIME each deploy_v2 ``type`` derives.

deploy_v2's coordinate is ``(service, type, version_ref, iac_ref)``; ``env`` is NOT an
input axis — it is derived from ``type`` (e.g. ``prod`` -> env ``prod``, ``preview/*`` ->
env ``preview``). This module owns that derived env regime and what it implies:

    type     -> deploy_contract.DEPLOY_TYPES (which env a type maps to)
    env      -> THIS module                  (which compose, URL, suffix, data default, gates)
    data     -> (P3/#893)                    (empty / staging / anonymized prod snapshot)

This module owns the env regime: each deploy environment maps to the Dokploy compose
it targets, its public URL pattern, the container/domain suffix, the default data
source, and prod gating. Single source so the deploy primitive, docs, and the contract
test all derive from here instead of re-stating per-env values across workflows.

The compose ids are mirrored from the App-repo workflows (staging-deploy.yml /
production-release.yml) and become the sole copy once P2 step 5 removes them there.
No deploy is performed here — like the resolver, this is pure, importable config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# data default per env. Non-prod defaults to `staging` data (operator choice); prod is
# always real prod data. A PR sha never runs on prod data and prod data never leaves
# prod un-anonymized (the G2 / RL-DATA red lines, finance_report#877) — those are
# enforced at the data axis (P3), not here.
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
#   branch-<name>   -> report-branch-<name>.<domain> (a branch tip, default main, on demand)
#   pr-<N>          -> report-pr-<N>.<domain>       (a specific PR)
#   commit-<sha7>   -> report-commit-<sha7>.<domain> (a pinned commit)
#   tag-<v1-2-3>    -> report-tag-<v1-2-3>.<domain>  (a release tag, DNS-safe slug)
#
# This is PURE config: no Dokploy calls, no resolution. preview_lifecycle.py
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

# the Dokploy project + environment the preview stacks live under (kept distinct
# from staging/prod composes; the lifecycle find-or-creates this environment).
PREVIEW_PROJECT = "finance_report"
PREVIEW_ENVIRONMENT = "preview"
# every preview compose name/appName shares this prefix so they are easy to find,
# list, and bulk-reason-about, and never collide with the staging/prod composes.
_PREVIEW_SLUG_PREFIX = "finance-report-preview"


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

    def app_url(self, *, domain: str) -> str:
        """Concrete public URL for this alias: https://report<suffix>.<domain>."""
        return f"https://report{self.env_suffix}.{domain}"


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


def preview_alias(kind: str, value: int | str | None = None) -> PreviewAlias:
    """Map a preview (kind, value) to its full deterministic identity.

    Pure and total over the validated surface; the single source the lifecycle and
    the tests both derive from. Every alias is uniformly ``<kind>-<slug>``. Examples:

        preview_alias("branch", "main") -> alias branch-main,     suffix -branch-main
        preview_alias("pr", 5)          -> alias pr-5,            suffix -pr-5
        preview_alias("commit", sha)    -> alias commit-1ab32d5,  suffix -commit-1ab32d5
        preview_alias("tag", "v1.2.3")  -> alias tag-v1-2-3,      suffix -tag-v1-2-3
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
        compose_name=f"{_PREVIEW_SLUG_PREFIX}-{alias}",
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
    # Preview branch aliases (e.g. report-main) — a concrete public URL each.
    for branch in _PREVIEW_CORS_BRANCHES:
        origins.append(f"https://report-{branch}.{domain}")
    # Preview pr/commit aliases — wildcard, since the value is per-deploy.
    for kind in _PREVIEW_CORS_WILDCARD_KINDS:
        origins.append(f"https://report-{kind}-*.{domain}")
    origins.append(_LOCAL_DEV_ORIGIN)
    return origins


def otel_ingest_endpoint(*, domain: str) -> str:
    """The public browser-OTLP traces endpoint for FE compose env (#368).

    Built from the SAME subdomain + path constants SigNoz's deploy.py uses
    (libs.common), so the FE endpoint and the ingest domain SigNoz registers can
    never disagree. The ingest domain is shared across envs (no env suffix), so
    this is independent of the deploy env — only the base domain varies.
    """
    from libs.common import OTEL_INGEST_SUBDOMAIN, OTLP_TRACES_PATH

    return f"https://{OTEL_INGEST_SUBDOMAIN}.{domain}{OTLP_TRACES_PATH}"


def otel_env(*, domain: str) -> dict[str, str]:
    """The OTLP compose-env contribution injected by every FE deploy path (#368).

    Single source for ``NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT`` so the two compose
    files consume the value instead of each re-constructing the URL inline.
    """
    return {
        "NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT": otel_ingest_endpoint(domain=domain)
    }
