"""Canonical domain and routing SSOT."""

from __future__ import annotations

import re
from dataclasses import dataclass

from infra2_sdk.config import EnvironmentTier, is_preview_alias

CANARY_SLOT = "canary-preview"
_LOCAL_DEV_ALIASES = frozenset({"dev", "development", "local", "local_dev"})
_LOCAL_TEST_ALIASES = frozenset({"test", "testing", "ci", "local_ci", "local_test"})
_PRODUCTION_ALIASES = frozenset({"prod", "production"})
_PREVIEW_ALIAS_RE = re.compile(
    r"\A(?:branch-[a-z0-9][a-z0-9_-]*|pr-[1-9][0-9]*|commit-[0-9a-f]{7,40}|tag-v[0-9]+-[0-9]+-[0-9]+)\Z"
)
_UNDERSCORE_PREVIEW_PREFIX_RE = re.compile(
    r"\A(?P<prefix>branch|pr|commit|tag)_(?P<rest>[a-z0-9][a-z0-9._-]*)\Z"
)


def _canonicalize_preview_alias(cleaned: str) -> str:
    if cleaned == "canary_preview":
        return CANARY_SLOT
    match = _UNDERSCORE_PREVIEW_PREFIX_RE.match(cleaned)
    if match is not None:
        rest = (
            match["rest"].replace("_", "-")
            if match["prefix"] == "tag"
            else match["rest"]
        )
        return f"{match['prefix']}-{rest}"
    return cleaned


def to_environment_tier(value: str | EnvironmentTier) -> EnvironmentTier:
    if isinstance(value, EnvironmentTier):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("pr", "canary") or cleaned.startswith("preview/"):
            return EnvironmentTier.PREVIEW
        normalized = cleaned.replace("-", "_")
        if normalized in _LOCAL_DEV_ALIASES:
            return EnvironmentTier.LOCAL_DEV
        if normalized in _LOCAL_TEST_ALIASES:
            return EnvironmentTier.LOCAL_TEST
        if normalized == EnvironmentTier.GITHUB_CI.value:
            return EnvironmentTier.GITHUB_CI
        alias = _canonicalize_preview_alias(cleaned)
        if (
            normalized == EnvironmentTier.PREVIEW.value
            or alias == CANARY_SLOT
            or _PREVIEW_ALIAS_RE.match(alias)
        ):
            return EnvironmentTier.PREVIEW
        if normalized == EnvironmentTier.STAGING.value:
            return EnvironmentTier.STAGING
        if normalized in _PRODUCTION_ALIASES:
            return EnvironmentTier.PRODUCTION
        raise ValueError(f"unknown environment: {value!r}")
    raise TypeError(f"cannot convert {type(value).__name__} to EnvironmentTier")


_DEFAULT_BASE_DOMAIN = "zitian.party"
_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]*\Z")


def resolve_app_hostname(
    slug: str,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = _DEFAULT_BASE_DOMAIN,
    slot_id: str | int | None = None,
    custom_prod_domain: str | None = None,
    is_canary: bool = False,
) -> str:
    slug = str(slug).strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r}")

    raw_tier = (
        str(tier).strip().lower()
        if not isinstance(tier, EnvironmentTier)
        else tier.value
    )
    if is_canary or raw_tier == "canary":
        return f"{slug}-{CANARY_SLOT}.{base_domain}"
    if slot_id is not None and str(slot_id).strip().lower() == CANARY_SLOT:
        return f"{slug}-{CANARY_SLOT}.{base_domain}"
    if is_preview_alias(raw_tier):
        return f"{slug}-{_canonicalize_preview_alias(raw_tier)}.{base_domain}"

    canonical_tier = to_environment_tier(tier)
    if canonical_tier is EnvironmentTier.PRODUCTION:
        return (
            custom_prod_domain.strip().lower()
            if custom_prod_domain
            else f"{slug}.{base_domain}"
        )
    if canonical_tier is EnvironmentTier.STAGING:
        return f"{slug}-staging.{base_domain}"
    if canonical_tier is EnvironmentTier.PREVIEW:
        if slot_id is not None:
            clean_slot = (
                f"pr-{slot_id}"
                if isinstance(slot_id, int)
                else str(slot_id).strip().lower()
            )
            if is_preview_alias(clean_slot):
                return f"{slug}-{_canonicalize_preview_alias(clean_slot)}.{base_domain}"
            return f"{slug}-preview-{clean_slot}.{base_domain}"
        return f"{slug}-preview.{base_domain}"
    return f"{slug}-{canonical_tier.value.replace('_', '-')}.{base_domain}"


def resolve_service_url(
    slug: str,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = _DEFAULT_BASE_DOMAIN,
    path: str = "/",
    slot_id: str | int | None = None,
    is_canary: bool = False,
    scheme: str = "https",
) -> str:
    host = resolve_app_hostname(
        slug, tier, base_domain=base_domain, slot_id=slot_id, is_canary=is_canary
    )
    return f"{scheme}://{host}{path if path.startswith('/') else f'/{path}'}"


@dataclass(frozen=True)
class PreviewStackCoordinates:
    slug: str
    tier: EnvironmentTier
    deployment_environment: str
    env_suffix: str
    env_domain_suffix: str
    app_host: str
    preview_url: str
    cpu_shares: int
    network_suffix: str
    ephemeral_database_url: str


def derive_preview_coordinates(
    slug: str,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = _DEFAULT_BASE_DOMAIN,
    slot_id: str | int | None = None,
    custom_prod_domain: str | None = None,
    is_canary: bool = False,
    scheme: str = "https",
) -> PreviewStackCoordinates:
    app_host = resolve_app_hostname(
        slug,
        tier,
        base_domain=base_domain,
        slot_id=slot_id,
        custom_prod_domain=custom_prod_domain,
        is_canary=is_canary,
    )
    raw_tier = (
        str(tier).strip().lower()
        if not isinstance(tier, EnvironmentTier)
        else tier.value
    )
    slot_str = str(slot_id).strip().lower() if slot_id is not None else None

    if is_canary or raw_tier == "canary" or slot_str == CANARY_SLOT:
        canonical_tier, deployment_environment, suffix, cpu_shares = (
            EnvironmentTier.PREVIEW,
            CANARY_SLOT,
            f"-{CANARY_SLOT}",
            256,
        )
    else:
        canonical_tier = to_environment_tier(tier)
        if canonical_tier is EnvironmentTier.PRODUCTION:
            deployment_environment, suffix, cpu_shares = "production", "", 1024
        elif canonical_tier is EnvironmentTier.STAGING:
            deployment_environment, suffix, cpu_shares = "staging", "-staging", 512
        else:
            if is_preview_alias(raw_tier):
                slot_label = _canonicalize_preview_alias(raw_tier)
            elif isinstance(slot_id, int):
                slot_label = f"pr-{slot_id}"
            elif slot_str and is_preview_alias(slot_str):
                slot_label = _canonicalize_preview_alias(slot_str)
            elif slot_str:
                slot_label = f"preview-{slot_str}"
            else:
                slot_label = "preview"
            deployment_environment, suffix, cpu_shares = (
                slot_label,
                f"-{slot_label}",
                256,
            )

    db_name = re.sub(r"[^a-z0-9_]", "_", slug.lower()) + "_preview"
    return PreviewStackCoordinates(
        slug=slug,
        tier=canonical_tier,
        deployment_environment=deployment_environment,
        env_suffix=suffix,
        env_domain_suffix=suffix,
        app_host=app_host,
        preview_url=f"{scheme}://{app_host}",
        cpu_shares=cpu_shares,
        network_suffix=suffix,
        ephemeral_database_url=f"postgresql://ephemeral:ephemeral@postgres:5432/{db_name}",
    )


__all__ = [
    "CANARY_SLOT",
    "PreviewStackCoordinates",
    "derive_preview_coordinates",
    "resolve_app_hostname",
    "resolve_service_url",
]
