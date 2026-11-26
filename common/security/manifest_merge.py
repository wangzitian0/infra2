"""Merge several EnvironmentManifests for one stack (#1320, absorbed from infra2-sdk v4.9.1)."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest

_RENDER_KEYS = (
    "source",
    "provided_by",
    "composed_from",
    "store_key",
    "empty_ok",
    "scope",
)


def merge_manifests(
    manifests: Sequence[tuple[str, EnvironmentManifest]],
    *,
    source: str,
    exclude: Collection[str] = (),
) -> EnvironmentManifest:
    """Union of several manifests of one stack, in order."""
    excluded = set(exclude)
    fields: dict[str, EnvironmentField] = {}
    for label, manifest in manifests:
        for entry in manifest.fields:
            if entry.env in excluded:
                continue
            existing = fields.get(entry.env)
            if existing is None:
                fields[entry.env] = entry
                continue
            for key in _RENDER_KEYS:
                if getattr(existing, key) != getattr(entry, key):
                    raise ValueError(
                        f"{source}: {entry.env}.{key} declared differently in {label}"
                    )
    return EnvironmentManifest(source=source, fields=tuple(fields.values()))


__all__ = ["merge_manifests"]
