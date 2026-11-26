"""Merge several EnvironmentManifests for one stack (#1320, absorbed from infra2-sdk)."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from infra2_sdk.config import EnvironmentManifest


def merge_manifests(
    manifests: Sequence[tuple[str, EnvironmentManifest]],
    *,
    source: str,
    exclude: Collection[str] = (),
) -> EnvironmentManifest:
    """Union of several manifests of one stack, in order."""
    return EnvironmentManifest.merge(manifests, source=source, exclude=exclude)


__all__ = ["merge_manifests"]
