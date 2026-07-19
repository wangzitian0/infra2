#!/usr/bin/env python3
"""The deploy_v2 contract layer behind the coordinate (service, type, version_ref, iac_ref).

This names a deploy's full identity. It does NOT deploy — it is pure/importable, the single
thing the deploy front door and the webhook validate against before any side effect.

The INPUT coordinate is 4 axes ``(service, type, version_ref, iac_ref)`` (see
``deploy_v2`` + SSOT §4.7); ``env`` / ``sub_domain`` are DERIVED from ``type`` and the
resolved slot, NOT inputs. The DERIVED identity this module builds is ``DeployTarget``:

    service       -> THIS module (ServiceSpec registry)
    type          -> DEPLOY_TYPES (the discriminant; derives env + alias slot + accepted_forms)
    env           -> deploy_env_config.env_config        (staging | prod | preview; from type)
    sub_domain    -> deploy_env_config (env suffix) + preview_alias (preview slot, <kind>-<value>)
    code_version  -> the resolved commit sha (deploy_v2 resolves version_ref -> sha + image_ref)
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
``report-branch-main`` / ``report-pr-N`` / ``report-commit-<sha>`` previews be addressed by name.

SSOT: docs/ssot/core.environments.md §4.7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from libs import service_registry
from libs.deploy_env_config import env_config, preview_alias

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
        not_yet_in_production: This service has no production deployment yet —
            its rollout is deliberately staging-scoped (#500/#542). Derived from
            the Deployer's literal ``not_yet_in_production`` attr; consumed by
            ``tools/reconcile_iac_inputs.py`` so a release-tag prod promotion
            never fans out to a service with nothing to deploy to (the v1.1.34
            2/14 prod-promote failure).
        env_shared: One endpoint shared across envs — no env suffix, no preview
            instances (e.g. vault/sso/signoz/minio).
        iac_pinned: A platform service whose artifact is the IaC-pinned stack at
            ``iac_ref`` (no app code version). ``deploy_v2`` routes these to the iac_runner
            ``/deploy`` webhook instead of the app backends, and ``version_ref`` is unused —
            the deploy ref IS ``iac_ref``. See SSOT §4.7.2.
        image_repositories: Container image repositories that must already expose the
            resolved ``image_ref`` before this service can be deployed. This is an artifact
            readiness dependency, distinct from the build/config fan-out graph.
        identity_service_name: The ``service.name`` telemetry label
            (``libs.service_identity.ServiceIdentity``) this app's deploy identity carries.
            Defaults to ``<service-part-of-key>`` with underscores dashed, matching the
            registry key unless the app's compose already established a different label.
        identity_component: The ``component`` telemetry label. Defaults to ``"app"``.
        supports_preview: Whether ``libs.deploy.preview`` can serve this service's
            preview/canary deploy types. False fails a preview/canary target closed. True
            requires a matching entry in
            ``libs.deploy_env_config.preview_service_config`` (project / compose path /
            DB name / base_subdomain, #522) — the preview lifecycle looks the service up
            there rather than assuming finance_report's internals.
    """

    key: str
    base_subdomain: str
    web_facing: bool
    prod_only: bool = False
    not_yet_in_production: bool = False
    env_shared: bool = False
    iac_pinned: bool = False
    image_repositories: tuple[str, ...] = ()
    identity_service_name: str = ""
    identity_component: str = "app"
    supports_preview: bool = True

    def resolved_identity_service_name(self) -> str:
        if self.identity_service_name:
            return self.identity_service_name
        return self.key.split("/", 1)[-1].replace("_", "-")


# finance_report and truealpha/app have a BESPOKE deploy_v2 backend (fixed-compose
# promote path via libs.deploy.promote; finance_report additionally supports the preview
# lifecycle via libs.deploy.preview — truealpha/app does not yet, #500). EVERY OTHER
# service is ``iac_pinned`` and routes to the iac_runner /deploy webhook — and its facts
# (subdomain, prod_only) are DERIVED from its deploy.py Deployer class via
# libs.service_registry, the single source of truth (Infra-013: never hand-copy service
# facts into a parallel list).
_APP_KEY = "finance_report/app"
_TRUEALPHA_APP_KEY = "truealpha/app"

SERVICES: dict[str, ServiceSpec] = {
    _APP_KEY: ServiceSpec(
        key=_APP_KEY,
        base_subdomain="report",
        web_facing=True,
        image_repositories=(
            "ghcr.io/wangzitian0/finance_report-backend",
            "ghcr.io/wangzitian0/finance_report-frontend",
        ),
        identity_service_name="finance-report-backend",
    ),
    # #500: version-pinned staging promotion (finance_report's fixed-compose path,
    # generalized). #522: preview/canary now wired too (libs.deploy.preview generalized
    # off a per-service registry — deploy_env_config.preview_service_config; truealpha's
    # own ephemeral-DB template is truealpha/truealpha/preview/compose.yaml). Production
    # went live manually 2026-07-19 (AppRole + secrets + MinIO bucket provisioned,
    # postgres + app deployed, health-checked at https://truealpha.zitian.party —
    # compose_id registered in deploy_env_config._APP_COMPOSE_OVERRIDES). The AUTOMATED
    # cross-repo path is still NOT wired: infra2 has no PRODUCTION_EVIDENCE_POLICIES
    # entry for this service (verify_production_evidence assumes finance_report's
    # single-repo/single-workflow model, which does not fit truealpha's delegated-
    # dispatch architecture) — a manual deploy_v2 invocation is the only supported
    # path to prod today. See infra2#522.
    _TRUEALPHA_APP_KEY: ServiceSpec(
        key=_TRUEALPHA_APP_KEY,
        base_subdomain="truealpha",
        web_facing=True,
        image_repositories=(
            "ghcr.io/wangzitian0/truealpha-app-web",
            "ghcr.io/wangzitian0/truealpha-llm-service",
        ),
        identity_service_name="truealpha-app",
        supports_preview=True,
    ),
}


@lru_cache(maxsize=1)
def _iac_pinned_specs() -> dict[str, ServiceSpec]:
    """Every non-app service, derived from the IaC Deployer classes (service_registry).

    These route to the iac_runner webhook; their subdomain / prod_only come straight from
    the deploy.py the iac_runner already syncs, so deploy_v2 can never drift from the deploy
    fan-out (no per-service hand-registration — all platform + app-backing services at once).
    """
    out: dict[str, ServiceSpec] = {}
    for sid, meta in service_registry.service_attrs().items():
        if sid in SERVICES:  # the app's bespoke spec wins
            continue
        out[sid] = ServiceSpec(
            key=sid,
            base_subdomain=meta.subdomain or meta.service,
            web_facing=meta.subdomain is not None,
            prod_only=meta.prod_only,
            not_yet_in_production=meta.not_yet_in_production,
            iac_pinned=True,
        )
    return out


def all_service_keys() -> list[str]:
    """Every deployable service deploy_v2 knows — the app + every iac_runner-synced service."""
    return sorted({*SERVICES, *_iac_pinned_specs()})


def service_spec(service: str) -> ServiceSpec:
    """Return the ServiceSpec for a service key. Raises ValueError if unknown.

    The app is explicit; every other service is derived from libs.service_registry (its
    deploy.py), so the registry is never a hand-maintained copy.
    """
    if service in SERVICES:
        return SERVICES[service]
    spec = _iac_pinned_specs().get(service)
    if spec is None:
        raise ValueError(
            f"unknown service {service!r}: expected one of {all_service_keys()}"
        )
    return spec


def sub_domain_for(
    spec: ServiceSpec, env: str, *, alias_kind: str | None = None, alias_value=None
) -> str:
    """The canonical ``sub_domain`` label for a target.

    staging/prod are pinned by ``env`` (``base`` + the env suffix); preview takes the
    alias (``branch-<name>`` / ``pr-<N>`` / ``commit-<sha7>`` / ``tag-<slug>``). ``env_shared`` services carry no
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
            raise ValueError(
                "preview requires an alias_kind (branch | pr | commit | tag)"
            )
        return (
            f"{spec.base_subdomain}{preview_alias(alias_kind, alias_value).env_suffix}"
        )
    if spec.env_shared:
        return spec.base_subdomain
    return f"{spec.base_subdomain}{cfg.env_suffix}"


@dataclass(frozen=True)
class DeployTarget:
    """The DERIVED identity of a deploy_v2 request (built from the 4-axis input coordinate)."""

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

    For preview, pass ``alias_kind`` (``branch`` | ``pr`` | ``commit`` | ``tag``) and
    ``alias_value``. Fails closed on any contract violation.
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
    RL-DATA-1 unreviewed-sha-never-on-prod-data): those derive from the target env's
    ``EnvConfig.data_default`` and live in the execution layer —
    ``deploy_v2.enforce_data_lane_red_lines``. ``data_lane`` is derived, not a public
    coordinate axis.
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
                f"{spec.base_subdomain!r} (expected {spec.base_subdomain}-branch-<name> / "
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
    # a preview slot is uniformly <kind>-<value>:
    #   -branch-<name> / -pr-<N> / -commit-<sha7> / -tag-<v1-2-3> (dots/slashes -> dashes).
    pattern = (
        rf"\A{re.escape(spec.base_subdomain)}-"
        r"(branch-[a-z0-9-]+|pr-[1-9][0-9]*|commit-[0-9a-f]{7}|tag-v[0-9]+-[0-9]+-[0-9]+)\Z"
    )
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
    """Static config a deploy ``type`` resolves to. env/alias/gates derive from here.

    ``accepted_forms`` is the type's slice of the (type × version_ref-form) matrix: the
    version_ref *forms* this type will accept, so a wrong combination (``prod`` + ``main``)
    fails closed instead of silently pulling a code image onto prod. Forms are the
    ``resolve_deploy_ref.classify_ref`` outputs (``branch`` / ``sha`` / ``tag``) plus
    ``pr`` (a PR number, resolved via ``resolve_pr``). Fixed envs (``staging`` / ``prod``)
    accept ``tag`` only — they deploy immutable release images, never a moving code ref.
    """

    key: str  # the type token, e.g. "prod" | "preview/pr"
    env: str  # underlying env regime (data-lane / secrets / lifecycle) — derived, not input
    alias_kind: str | None = (
        None  # preview alias kind (branch|pr|commit|tag); None = fixed env
    )
    accepted_forms: tuple[
        str, ...
    ] = ()  # version_ref forms this type accepts (matrix row)

    # NOTE: the gates are NOT re-declared here — they derive from the type's env, the single
    # source of truth: staging-first is ``env_config(env).requires_staging_first`` and the
    # RL-DATA-1 review gate keys on ``data_lane == "prod"`` (also env-derived). Adding a
    # bool here would be a second, drift-prone copy of a policy the env already owns.


# The form vocabulary (resolve_deploy_ref.classify_ref outputs + the PR-number form).
_CODE_FORMS = (
    "branch",
    "sha",
)  # an app commit: main tip / a pinned sha -> short-sha image
_RELEASE_FORMS = ("tag",)  # a retained, immutable release image: vX.Y.Z
_ALL_REF_FORMS = _CODE_FORMS + _RELEASE_FORMS

# The closed set of deploy types. Adding a scenario = adding one entry here (open-closed);
# existing types are untouched. `canary` is an EXPLICIT type (guardrail: never an emergent
# property of "empty params") — it is a preview deploy whose execution adds health+teardown.
#
# accepted_forms encodes the (type × form) matrix:
#   staging        — RELEASE tag only; staging mirrors prod (promote-not-rebuild), so a
#                    code ref (branch/sha) fails closed just like prod.
#   prod           — RELEASE tag only (vX.Y.Z); a code ref fails closed.
#   preview/branch — a branch tip (default main) -> report-branch-<name>.
#   preview/pr     — a PR number (resolve_pr -> the PR head image) -> report-pr-<N>.
#   preview/commit — a pinned commit sha -> report-commit-<sha7>.
#   preview/tag    — a release tag -> report-tag-<slug>.
#   canary         — any ref form, code OR release (_ALL_REF_FORMS); runs on a fixed
#                    throwaway pr-<N> slot (a deploy-path probe, kept maximally flexible).
# Every preview slot is uniformly <kind>-<value>; `branch` (not a bare `main`) is what makes
# that uniform — so downstream slot parsing/generation never special-cases the main tip.
DEPLOY_TYPES: dict[str, DeployTypeSpec] = {
    "staging": DeployTypeSpec("staging", env="staging", accepted_forms=_RELEASE_FORMS),
    "prod": DeployTypeSpec("prod", env="prod", accepted_forms=_RELEASE_FORMS),
    "preview/branch": DeployTypeSpec(
        "preview/branch", env="preview", alias_kind="branch", accepted_forms=("branch",)
    ),
    "preview/pr": DeployTypeSpec(
        "preview/pr", env="preview", alias_kind="pr", accepted_forms=("pr",)
    ),
    "preview/commit": DeployTypeSpec(
        "preview/commit", env="preview", alias_kind="commit", accepted_forms=("sha",)
    ),
    "preview/tag": DeployTypeSpec(
        "preview/tag", env="preview", alias_kind="tag", accepted_forms=("tag",)
    ),
    "canary": DeployTypeSpec(
        "canary", env="preview", alias_kind="pr", accepted_forms=_ALL_REF_FORMS
    ),
}


def deploy_type_spec(deploy_type: str) -> DeployTypeSpec:
    """Resolve a deploy type to its spec. Raises ValueError for an unknown type (closed set)."""
    try:
        return DEPLOY_TYPES[deploy_type]
    except KeyError:
        raise ValueError(
            f"unknown deploy type {deploy_type!r}: expected one of {sorted(DEPLOY_TYPES)}"
        ) from None


def validate_ref_form(deploy_type: str, form: str) -> None:
    """Fail closed when a deploy type is handed a version_ref form it does not accept.

    This is the (type × form) matrix made enforceable: ``prod`` + a ``branch``/``sha`` ref
    raises here rather than letting a code image reach prod. ``form`` is a
    ``classify_ref`` output or ``pr``.
    """
    spec = deploy_type_spec(deploy_type)
    if form not in spec.accepted_forms:
        raise ValueError(
            f"deploy type {deploy_type!r} does not accept a {form!r} version_ref "
            f"(accepts {list(spec.accepted_forms)})"
        )


# Fixed envs pin their IaC to an immutable release tag, NOT a moving sha/branch. This is the
# iac_ref counterpart to validate_ref_form: it is a separate axis from version_ref (a preview
# legitimately clones a branch/sha iac_ref), so it keys on the type's env rather than the
# version_ref form matrix.
_TAG_ONLY_IAC_ENVS = ("staging", "prod")


def validate_iac_ref_form(deploy_type: str, iac_form: str) -> None:
    """Fail closed when a fixed env (staging/prod) is handed a non-tag ``iac_ref``.

    ``iac_form`` is a ``classify_ref`` output. staging/prod must pin IaC to a release tag
    (``vX.Y.Z``); a ``branch``/``sha`` iac_ref raises here rather than letting a moving
    infra2 ref reach a fixed env. preview/canary accept any iac_ref form (they clone live
    refs), so this is a no-op for them.
    """
    spec = deploy_type_spec(deploy_type)
    if spec.env in _TAG_ONLY_IAC_ENVS and iac_form != "tag":
        raise ValueError(
            f"deploy type {deploy_type!r} (env {spec.env!r}) requires a release-tag "
            f"iac_ref (vX.Y.Z), got a {iac_form!r} ref"
        )


def is_tag_only_iac_env(deploy_type: str) -> bool:
    """True for fixed envs (staging/prod) whose iac_ref must be a release tag — and, per
    ``deploy_v2.assert_iac_ref_on_main``, an *on-main* one. False for preview/canary."""
    return deploy_type_spec(deploy_type).env in _TAG_ONLY_IAC_ENVS


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

    ``version`` is the RESOLVED 40-hex commit identity (forwarded to ``code_version``,
    which ``validate_deploy_target`` enforces). The polymorphic ``version_ref`` surface
    (PR# / sha / tag / branch) and its per-type form gating live one layer up in
    ``deploy_v2`` (``resolve_image_ref`` / ``resolve_pr`` + ``validate_ref_form``); by the
    time a target is built the ref is already a sha and the image_ref is decided.

    ``alias_value`` is meaningful for every preview type (``preview/branch`` carries the
    branch name, ``preview/pr`` the number, ``preview/commit`` the sha, ``preview/tag`` the
    tag, ``canary`` its reserved slot). Only the fixed envs (``staging`` / ``prod``,
    ``alias_kind=None``) reject it, so the discriminated-union surface stays fail-closed
    rather than silently ignoring a caller mistake.
    """
    spec = deploy_type_spec(deploy_type)
    if alias_value is not None and spec.alias_kind is None:
        raise ValueError(
            f"deploy type {deploy_type!r} takes no alias_value (it is a fixed env)"
        )
    return make_deploy_target(
        service=service,
        env=spec.env,
        code_version=version,
        iac_ref=iac_ref,
        alias_kind=spec.alias_kind,
        alias_value=alias_value,
    )
