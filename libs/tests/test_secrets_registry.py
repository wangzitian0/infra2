"""libs/secrets_registry: the one list of manifests behind rendering, supply and reconcile."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from libs import secrets_registry
from libs.secrets_registry import SERVICES, Service


def test_every_registered_manifest_is_resolvable() -> None:
    for service in SERVICES:
        for path in service.manifests:
            assert secrets_registry.manifest_file(path).exists(), (
                f"{service.id}: {path}"
            )


def test_service_ids_are_unique_per_preview_flavour() -> None:
    counts = Counter((s.project, s.service, s.preview) for s in SERVICES)
    assert [key for key, n in counts.items() if n > 1] == []


def test_lookup_distinguishes_fixed_stacks_from_previews() -> None:
    fixed = secrets_registry.lookup("truealpha", "app")
    preview = secrets_registry.lookup("truealpha", "app", preview=True)
    assert fixed is not None and fixed.directory == "truealpha/truealpha/10.app"
    assert preview is not None and preview.source_env == "staging"
    assert "DATABASE_URL" in preview.exclude_envs
    assert secrets_registry.lookup("platform", "nonesuch") is None


def _write(tmp_path, rel: str, fields: list[dict]) -> None:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"contract_version": 2, "source": rel, "fields": fields}),
        encoding="utf-8",
    )


def test_merged_manifest_unions_agreeing_declarations_and_drops_excluded_envs(
    tmp_path,
) -> None:
    db = {"field": "database_url", "env": "DATABASE_URL", "source": "runtime"}
    _write(
        tmp_path, "a.json", [db, {"field": "key_a", "env": "KEY_A", "source": "human"}]
    )
    _write(
        tmp_path, "b.json", [db, {"field": "key_b", "env": "KEY_B", "source": "code"}]
    )
    service = Service("x", "p", "s", ("a.json", "b.json"), exclude_envs=("KEY_B",))
    merged = secrets_registry.merged_manifest(service, root=tmp_path)
    assert [f.env for f in merged.fields] == ["DATABASE_URL", "KEY_A"]


def test_merged_manifest_rejects_conflicting_render_attributes(tmp_path) -> None:
    _write(tmp_path, "a.json", [{"field": "k", "env": "K", "source": "runtime"}])
    _write(tmp_path, "b.json", [{"field": "k", "env": "K", "source": "human"}])
    service = Service("x", "p", "s", ("a.json", "b.json"))
    with pytest.raises(ValueError, match="K.source declared differently"):
        secrets_registry.merged_manifest(service, root=tmp_path)


def test_app_manifests_fall_back_to_the_ci_cache(tmp_path) -> None:
    rel = "repos/truealpha/apps/x/required-env.generated.json"
    _write(tmp_path, f"{secrets_registry.CACHE_DIR}/{rel}", [])
    assert (
        secrets_registry.manifest_file(rel, root=tmp_path)
        == tmp_path / secrets_registry.CACHE_DIR / rel
    )
    assert (
        secrets_registry.manifest_file("platform/x.json", root=tmp_path)
        == tmp_path / "platform/x.json"
    )
