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


def test_load_manifest_fetches_an_absent_app_manifest_at_the_pinned_commit(
    tmp_path,
) -> None:
    """The iac-runner and CI have no submodule checkout; the registry must still resolve
    app manifests at deploy time (the supply reads them) by fetching at the gitlink sha."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    sha = "0123456789abcdef0123456789abcdef01234567"
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{sha},repos/truealpha",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "pin"], check=True
    )
    urls: list[str] = []

    def fake(url: str) -> bytes:
        urls.append(url)
        return json.dumps(
            {
                "contract_version": 2,
                "source": "t",
                "fields": [{"field": "k", "env": "K", "source": "runtime"}],
            }
        ).encode()

    rel = "repos/truealpha/apps/x/required-env.generated.json"
    manifest = secrets_registry.load_manifest(rel, root=tmp_path, fetch=fake)
    assert [f.env for f in manifest.fields] == ["K"]
    assert urls == [
        f"https://raw.githubusercontent.com/wangzitian0/truealpha/{sha}/apps/x/required-env.generated.json"
    ]
    # cached: the next read makes no request
    secrets_registry.load_manifest(rel, root=tmp_path, fetch=fake)
    assert len(urls) == 1


def test_every_provided_by_names_a_store_backed_field_of_that_provider() -> None:
    """The prune keeps a provider's key because the provider declares it; if a consumer
    named a key its provider does not own, the prune would delete a live credential."""
    from infra2_sdk.secrets import vault_path  # noqa: F401  (documents the path shape)

    by_id = {service.id: service for service in SERVICES}
    for service in SERVICES:
        for field in secrets_registry.merged_manifest(service).fields:
            if not field.provided_by:
                continue
            provider_id, _, key = field.provided_by.partition(":")
            assert provider_id in by_id, (
                f"{service.id}: {field.env} names unknown {provider_id}"
            )
            provider_keys = secrets_registry.store_keys(by_id[provider_id])
            assert key in provider_keys, (
                f"{service.id}: {field.env} reads {provider_id}:{key}, which that service "
                "does not declare as store-backed"
            )
