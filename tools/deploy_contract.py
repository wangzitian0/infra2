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
from dataclasses import dataclass, field

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
    # Fixed-env Dokploy compose ids, per env (e.g. {"staging": "...", "prod": "..."}).
    # The dynamic preview env has no fixed compose (it is created per alias), so it never
    # appears here. The finance_report app keeps its ids in deploy_env_config for now; a
    # multi-service like `finance_report/canary` carries its own here.
    compose_ids: dict = field(default_factory=dict)


# Seed registry. Today the deploy primitive (tools/deploy_primitive.py) covers exactly
# the finance_report app stack; platform services deploy via libs/deployer.py and join
# this registry when the unified front door dispatches across both paths (next phase).
SERVICES: dict[str, ServiceSpec] = {
    "finance_report/app": ServiceSpec(
        key="finance_report/app", base_subdomain="report", web_facing=True
    ),
    # The canary is a first-class SERVICE, not a deploy type: a trivial 200-returning
    # probe with NO data/vault/DB deps, deployable to every env via the normal types.
    # Deploying it validates the deploy MECHANISM (resolve → contract → backend → route →
    # 200) for an env without touching the real app stack — the only way to get a
    # repeatable canary in the fixed staging/prod envs. Its fixed-env compose ids are
    # filled in once the per-env canary composes are provisioned (empty = not yet, and
    # compose_id_for fails closed). web_facing so it gets a Traefik route to probe.
    "finance_report/canary": ServiceSpec(
        key="finance_report/canary", base_subdomain="canary", web_facing=True
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


def compose_id_for(service: str, env: str) -> str:
    """The fixed Dokploy compose id for a ``(service, env)``. Fail-closed if absent.

    The finance_report app keeps its ids in ``deploy_env_config`` (env_config(env)
    .compose_id); other services carry their own in ``ServiceSpec.compose_ids``. Raises
    ValueError for a dynamic env (preview has no fixed compose) or an unprovisioned
    ``(service, env)`` — so deploying e.g. the canary to a not-yet-provisioned env fails
    closed with a clear message rather than a cryptic Dokploy error.
    """
    spec = service_spec(service)
    cfg = env_config(env)  # also validates env is known
    if cfg.dynamic:
        raise ValueError(f"{env!r} is dynamic (per-alias); it has no fixed compose id")
    compose_id = spec.compose_ids.get(env)
    if compose_id is None and service == "finance_report/app":
        compose_id = cfg.compose_id  # the app's ids still live in env_config
    if not compose_id:
        raise ValueError(
            f"no compose provisioned for service {service!r} in env {env!r}; "
            "provision the fixed compose before deploying"
        )
    return compose_id


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
    """The COMPLETE per-type contract: a deploy ``type`` fully declares what it accepts.

    A caller (this app, or any other app) reads its type's spec to know exactly which
    parameters to pass — env/alias derive from the type, ``accepted_forms`` says which
    version ref-forms are legal, and the gate flags say which red lines apply. Wrong input
    fast-fails against this spec.

    Fields:
        key:          the type token (e.g. "prod" | "preview/pr").
        env:          derived env regime (data-lane / secrets / lifecycle).
        alias_kind:   preview alias kind (main|pr|commit|tag); None for fixed envs.
        accepted_forms: classify_ref forms this type accepts — ``branch`` (main) / ``sha``
            / ``tag`` (vX.Y.Z) / ``release-branch`` (release/x.y). The image REFERENCE a
            form resolves to is the App's contract (resolve_deploy_ref), not gated here;
            this only gates which surface inputs are legal for the type.
        requires_review: RL-DATA-1 — caller must assert code_reviewed=True.
        requires_staging_first: promote-not-rebuild — caller must assert staging_validated
            (or break_glass).
    """

    key: str
    env: str
    alias_kind: str | None = None
    accepted_forms: frozenset = field(default_factory=frozenset)
    requires_review: bool = False
    requires_staging_first: bool = False


# The two image-addressing classes (which the FORM, not the type, decides):
#   code refs  (main / commit sha)      -> the App publishes a short-sha image
#   release refs (vX.Y.Z / release/x.y) -> the App publishes/retains a tag image
_CODE_FORMS = frozenset({"branch", "sha"})
_RELEASE_FORMS = frozenset({"tag", "release-branch"})

# The closed set of deploy types — each a complete contract. Adding a scenario = one entry
# (open-closed). Per-type usage examples (the homework other apps copy):
#
#   preview/main   : deploy_v2(type="preview/main",  service=S, version="main",   iac_ref=I, domain=D)
#   preview/pr     : deploy_v2(type="preview/pr",    service=S, version=<sha>,    iac_ref=I, domain=D, alias_value=7)
#   preview/commit : deploy_v2(type="preview/commit",service=S, version=<sha>,    iac_ref=I, domain=D, alias_value=<sha7>)
#   staging        : deploy_v2(type="staging",       service=S, version="main",   iac_ref=I, domain=D)   # or version="v1.2.3"
#   prod           : deploy_v2(type="prod",          service=S, version="v1.2.3", iac_ref=I, domain=D,
#                              staging_validated=True, code_reviewed=True)         # gates required
#   canary         : run_canary(...)  # = type "canary" (a self-test preview), code refs only
DEPLOY_TYPES: dict[str, DeployTypeSpec] = {
    "preview/main": DeployTypeSpec(
        "preview/main",
        env="preview",
        alias_kind="main",
        accepted_forms=frozenset({"branch"}),  # the report-main slot tracks main only
    ),
    "preview/pr": DeployTypeSpec(
        "preview/pr",
        env="preview",
        alias_kind="pr",
        accepted_forms=frozenset({"sha"}),  # the PR's head commit
    ),
    "preview/commit": DeployTypeSpec(
        "preview/commit",
        env="preview",
        alias_kind="commit",
        accepted_forms=frozenset({"sha"}),
    ),
    "staging": DeployTypeSpec(
        "staging",
        env="staging",
        accepted_forms=_CODE_FORMS | _RELEASE_FORMS,  # universal soak: code OR release
    ),
    "prod": DeployTypeSpec(
        "prod",
        env="prod",
        accepted_forms=_RELEASE_FORMS,  # releases only — never main / bare sha
        requires_review=True,
        requires_staging_first=True,
    ),
    # canary = an explicit self-test preview (infra2 mechanism probe), code refs only.
    "canary": DeployTypeSpec(
        "canary",
        env="preview",
        alias_kind="pr",
        accepted_forms=_CODE_FORMS,
    ),
}


def validate_ref_form(deploy_type: str, ref: str) -> str:
    """Fail-closed if ``ref``'s surface form is not accepted by the type; return the form.

    Gates which surface inputs a type accepts (e.g. ``prod`` accepts only release forms
    ``vX.Y.Z`` / ``release/x.y``, never ``main`` or a bare sha). The form is pure
    pattern-matching (classify_ref); the image reference it resolves to is the App's
    contract, not decided here.
    """
    from tools.resolve_deploy_ref import classify_ref

    spec = deploy_type_spec(deploy_type)
    form = classify_ref(ref)
    if form not in spec.accepted_forms:
        raise ValueError(
            f"deploy type {deploy_type!r} does not accept a {form!r} ref ({ref!r}); "
            f"accepted forms: {sorted(spec.accepted_forms)}"
        )
    return form


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
