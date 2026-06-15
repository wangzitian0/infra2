#!/usr/bin/env python3
"""The deploy_v2 coordinate: deploy(service, env, sub_domain, code_version, iac_ref).

This is the contract layer that names a deploy's full identity. It does NOT deploy —
it is pure/importable, the single thing the deploy front door and the webhook validate
against before any side effect.

It layers the two new axes (``service``, ``iac_ref``) and a unified ``sub_domain`` over
the existing Infra-009 axes, reusing them rather than restating:

    service       -> THIS module (ServiceSpec registry)
    env           -> deploy_env_config.env_config        (staging | prod | preview)
    sub_domain    -> deploy_env_config (env suffix) + preview_alias (preview slot)
    code_version  -> resolve_deploy_ref (a commit sha; resolved at execution)
    iac_ref       -> THIS module (a 40-hex infra2 commit pinning the IaC)

``data`` is NOT a sixth input axis: it is derived (``EnvConfig.data_default``, optionally
pinned by the IaC at ``iac_ref``) and only appears in the red-line predicates
(``env=prod => data_lane=prod``; an unreviewed PR sha never on prod data) — see
deploy_env_config and finance_report#893.

Why ``service`` and ``iac_ref`` are independent axes (necessary & sufficient): the image
comes from the app repo (``code_version``) while the compose/env wiring comes from infra2
(``iac_ref``) — two refs that drift independently; and infra2 is multi-service, so what to
deploy is its own dimension. ``sub_domain`` is the stack-instance label: pinned by ``env``
for staging/prod, free (the preview alias) for preview — this is what lets the coexisting
``report-main`` / ``report-pr-N`` / ``report-commit-<sha>`` previews be addressed by name.

SSOT: docs/ssot/core.environments.md §4.7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tools.deploy_env_config import env_config, preview_alias

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")  # a resolved, image-addressable commit sha


@dataclass(frozen=True)
class ServiceSpec:
    """Static description of a deployable service — needed to validate a target.

    Attributes:
        key: Registered service key, e.g. ``finance_report/app``.
        base_subdomain: Stack alias base (single DNS label), e.g. ``report``. Every
            deployable service has one even if it is not publicly routed.
        web_facing: Publishes a Traefik route at ``{sub_domain}.{domain}``. Backing
            services (postgres/redis/…) are ``False``: ``sub_domain`` still derives
            ENV_SUFFIX/DATA_PATH but is not routed.
        prod_only: Only deployable to ``prod`` (e.g. clickhouse/signoz/openpanel).
        env_shared: One endpoint shared across envs — no env suffix, no preview
            instances (e.g. vault/sso/signoz/minio).
    """

    key: str
    base_subdomain: str
    web_facing: bool
    prod_only: bool = False
    env_shared: bool = False


# Seed registry. Today the deploy primitive (tools/deploy_primitive.py) covers exactly
# the finance_report app stack; platform services deploy via libs/deployer.py and join
# this registry when the unified front door dispatches across both paths (next phase).
SERVICES: dict[str, ServiceSpec] = {
    "finance_report/app": ServiceSpec(
        key="finance_report/app", base_subdomain="report", web_facing=True
    ),
}


def service_spec(service: str) -> ServiceSpec:
    """Return the ServiceSpec for a service key. Raises ValueError if unregistered."""
    try:
        return SERVICES[service]
    except KeyError:
        raise ValueError(
            f"unknown service {service!r}: expected one of {sorted(SERVICES)}"
        ) from None


def sub_domain_for(
    spec: ServiceSpec, env: str, *, alias_kind: str | None = None, alias_value=None
) -> str:
    """The canonical ``sub_domain`` label for a target.

    staging/prod are pinned by ``env`` (``base`` + the env suffix); preview takes the
    alias (``main`` / ``pr-<N>`` / ``commit-<sha7>``). ``env_shared`` services carry no
    suffix on any env.
    """
    cfg = env_config(env)
    if cfg.dynamic:  # preview
        # Surface the structural reason first: a prod-only / env-shared service has no
        # preview slot at all, so report that rather than masking it behind a missing
        # alias_kind (which a caller cannot satisfy anyway).
        if spec.prod_only or spec.env_shared:
            kind = "prod_only" if spec.prod_only else "env_shared"
            raise ValueError(f"{spec.key} has no preview instances ({kind})")
        if alias_kind is None:
            raise ValueError("preview requires an alias_kind (main | pr | commit)")
        return (
            f"{spec.base_subdomain}{preview_alias(alias_kind, alias_value).env_suffix}"
        )
    if spec.env_shared:
        return spec.base_subdomain
    return f"{spec.base_subdomain}{cfg.env_suffix}"


@dataclass(frozen=True)
class DeployTarget:
    """A fully-specified deploy_v2 request — the five orthogonal axes."""

    service: str
    env: str  # staging | prod | preview
    sub_domain: str
    code_version: str  # 40-hex commit sha of the app code
    iac_ref: str  # 40-hex commit sha of infra2 (pins the IaC)

    def to_dict(self) -> dict[str, str]:
        return {
            "service": self.service,
            "env": self.env,
            "sub_domain": self.sub_domain,
            "code_version": self.code_version,
            "iac_ref": self.iac_ref,
        }


def make_deploy_target(
    *,
    service: str,
    env: str,
    code_version: str,
    iac_ref: str,
    alias_kind: str | None = None,
    alias_value=None,
) -> DeployTarget:
    """Build and validate a :class:`DeployTarget`, deriving the canonical sub_domain.

    For preview, pass ``alias_kind`` (``main`` | ``pr`` | ``commit``) and ``alias_value``.
    Fails closed on any contract violation.
    """
    spec = service_spec(service)
    sub_domain = sub_domain_for(
        spec, env, alias_kind=alias_kind, alias_value=alias_value
    )
    target = DeployTarget(
        service=service,
        env=env,
        sub_domain=sub_domain,
        code_version=code_version.strip().lower(),
        iac_ref=iac_ref.strip().lower(),
    )
    validate_deploy_target(target, spec)
    return target


def validate_deploy_target(target: DeployTarget, spec: ServiceSpec) -> None:
    """Enforce the SSOT §4.7 *contract* predicates. Raises ``ValueError`` on first miss.

    This validates the contract axes only — service registration, 40-hex sha shapes for
    ``code_version``/``iac_ref``, env legality, and the ``sub_domain`` ↔ env/preview-slot
    derivation. It does NOT enforce the §5 data-lane red lines (``env=prod => data_lane``;
    RL-DATA-1 unreviewed-sha-never-on-prod-data): those derive from ``iac_ref`` and live in
    the execution layer — ``deploy_v2.enforce_data_lane_red_lines`` — and become fully
    data-aware when the data axis lands (finance_report#893).
    """
    if spec.key != target.service:
        raise ValueError(
            f"spec.key {spec.key!r} does not match service {target.service!r}"
        )
    if not _SHA_RE.match(target.code_version):
        raise ValueError("code_version must be a 40-hex commit sha")
    if not _SHA_RE.match(target.iac_ref):
        raise ValueError("iac_ref must be a 40-hex commit sha")

    cfg = env_config(target.env)  # also validates env is known

    # prod-only services only reach prod.
    if spec.prod_only and target.env != "prod":
        raise ValueError(f"{spec.key} is prod-only; cannot deploy to {target.env}")

    if cfg.dynamic:  # preview
        if spec.prod_only or spec.env_shared:
            kind = "prod_only" if spec.prod_only else "env_shared"
            raise ValueError(f"{spec.key} has no preview instances ({kind})")
        # sub_domain must be base + a valid preview alias suffix, and must not collide
        # with a real env's domain.
        if not _is_valid_preview_sub_domain(spec, target.sub_domain):
            raise ValueError(
                f"sub_domain {target.sub_domain!r} is not a valid preview slot for "
                f"{spec.base_subdomain!r} (expected {spec.base_subdomain}-main / "
                f"-pr-<N> / -commit-<sha7>)"
            )
        for env_name in ("staging", "prod"):
            if target.sub_domain == _pinned_sub_domain(spec, env_name):
                raise ValueError(
                    f"preview sub_domain must not equal the {env_name} domain"
                )
        return

    # staging/prod: sub_domain is pinned by env.
    expected = _pinned_sub_domain(spec, target.env)
    if target.sub_domain != expected:
        raise ValueError(
            f"env={target.env} requires sub_domain={expected!r}, got {target.sub_domain!r}"
        )


def _pinned_sub_domain(spec: ServiceSpec, env: str) -> str:
    cfg = env_config(env)
    if spec.env_shared:
        return spec.base_subdomain
    return f"{spec.base_subdomain}{cfg.env_suffix}"


def _is_valid_preview_sub_domain(spec: ServiceSpec, sub_domain: str) -> bool:
    pattern = rf"\A{re.escape(spec.base_subdomain)}-(main|pr-[1-9][0-9]*|commit-[0-9a-f]{{7}})\Z"
    return re.match(pattern, sub_domain) is not None


# ---------------------------------------------------------------------------
# The deploy TYPE — the discriminant ("one primitive, N scenarios")
# ---------------------------------------------------------------------------
# `type` is the PRIMARY axis: it names the scenario and decides how the other axes are
# interpreted and which are required (a discriminated union). The env regime
# (data-lane / secrets / lifecycle) and the sub_domain are DERIVED from the type — `env`
# is no longer a separate input axis, it is a property of the type. Per SSOT §4.7.
#
# Three framework guardrails (not business-specific — kept stable):
#   1. type selects a STRATEGY/config, not a flat enum with embedded values. Per-instance
#      data (a PR number) rides as `alias_value`, so the type set stays small.
#   2. one common core + per-type config: resolve type -> spec ONCE, then shared code runs
#      (no scattered `switch(type)`).
#   3. fail-closed by construction: an unknown type is rejected; each type's spec declares
#      what it requires.
#
# BUSINESS-TBD (left as placeholders, see §4.7): how `version` is interpreted per type
# (code sha for previews, release tag for prod, or a business-defined version) and the
# canary's execution defaults. The contract carries the version as-is for now.


@dataclass(frozen=True)
class DeployTypeSpec:
    """Static config a deploy ``type`` resolves to. env/alias/gates derive from here."""

    key: str  # the type token, e.g. "prod" | "preview/pr"
    env: str  # underlying env regime (data-lane / secrets / lifecycle) — derived, not input
    alias_kind: str | None = (
        None  # preview alias kind (main|pr|commit); None = fixed env
    )
    requires_review: bool = (
        False  # RL-DATA-1: prod-data types require code_reviewed=True
    )


# The closed set of deploy types. Adding a scenario = adding one entry here (open-closed);
# existing types are untouched. `canary` is an EXPLICIT type (guardrail: never an emergent
# property of "empty params") — it is a preview deploy whose execution adds health+teardown.
DEPLOY_TYPES: dict[str, DeployTypeSpec] = {
    "staging": DeployTypeSpec("staging", env="staging"),
    "prod": DeployTypeSpec("prod", env="prod", requires_review=True),
    "preview/main": DeployTypeSpec("preview/main", env="preview", alias_kind="main"),
    "preview/pr": DeployTypeSpec("preview/pr", env="preview", alias_kind="pr"),
    "preview/commit": DeployTypeSpec(
        "preview/commit", env="preview", alias_kind="commit"
    ),
    "canary": DeployTypeSpec("canary", env="preview", alias_kind="pr"),
}


def deploy_type_spec(deploy_type: str) -> DeployTypeSpec:
    """Resolve a deploy type to its spec. Raises ValueError for an unknown type (closed set)."""
    try:
        return DEPLOY_TYPES[deploy_type]
    except KeyError:
        raise ValueError(
            f"unknown deploy type {deploy_type!r}: expected one of {sorted(DEPLOY_TYPES)}"
        ) from None


def make_target(
    deploy_type: str,
    *,
    service: str,
    version: str,
    iac_ref: str,
    alias_value=None,
) -> DeployTarget:
    """Type-first builder: ``type`` drives env + sub_domain derivation, then validates.

    The five-axis :class:`DeployTarget` is the DERIVED identity; the type-first surface is
    ``(type, service, version, iac_ref [, alias_value])``.

    ``version`` — CURRENTLY a 40-hex commit sha (forwarded to ``code_version``, which
    ``validate_deploy_target`` enforces). Tags (e.g. ``v1.2.3``) are NOT yet accepted —
    the per-type version interpretation (sha for preview, release tag for prod) is
    business-TBD (§4.7.1).

    ``alias_value`` is only meaningful for the per-instance preview types
    (``preview/pr`` / ``preview/commit`` / ``canary``). For fixed envs and ``preview/main``
    (which carry no alias value) it is rejected, so the discriminated-union surface stays
    fail-closed rather than silently ignoring a caller mistake.
    """
    spec = deploy_type_spec(deploy_type)
    if alias_value is not None and spec.alias_kind in (None, "main"):
        raise ValueError(
            f"deploy type {deploy_type!r} takes no alias_value "
            f"(alias_kind={spec.alias_kind!r})"
        )
    return make_deploy_target(
        service=service,
        env=spec.env,
        code_version=version,
        iac_ref=iac_ref,
        alias_kind=spec.alias_kind,
        alias_value=alias_value,
    )
