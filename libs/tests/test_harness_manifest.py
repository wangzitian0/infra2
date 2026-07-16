from __future__ import annotations

import json
from pathlib import Path

import yaml

from libs.harness_manifest import (
    HarnessManifestError,
    check_workspace,
    load_manifest,
    validate_manifest,
)
from tools.harness import main

ROOT = Path(__file__).resolve().parents[2]


def _valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "workspace": {
            "id": "test-harness",
            "focus": ["infra2"],
            "preferences": ["preference.md"],
        },
        "repositories": [
            {
                "id": "infra2",
                "path": ".",
                "checkout": "root",
                "role": "infrastructure-control-plane",
                "governance": "local",
                "source": "https://example.test/infra2",
                "release_identity": "tag",
                "authority": ["AGENTS.md"],
            },
            {
                "id": "app",
                "path": "repos/app",
                "checkout": "submodule",
                "role": "external-application",
                "governance": "autonomous",
                "source": "https://example.test/app",
                "release_identity": "image",
                "authority": ["AGENTS.md"],
            },
        ],
    }


def _workspace(tmp_path: Path) -> dict:
    (tmp_path / "preference.md").write_text("preference", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("authority", encoding="utf-8")
    app = tmp_path / "repos" / "app"
    app.mkdir(parents=True)
    (app / "AGENTS.md").write_text("authority", encoding="utf-8")
    return _valid_manifest()


def test_committed_inventory_is_valid_and_apps_are_autonomous() -> None:
    manifest = load_manifest(ROOT / "harness" / "repos.yaml")
    result = validate_manifest(ROOT, manifest)

    assert result.ok, result.to_dict()
    assert result.repository_count == 4
    assert manifest["workspace"]["focus"] == ["infra2", "infra2-sdk"]
    apps = [
        repo
        for repo in manifest["repositories"]
        if repo["role"] == "external-application"
    ]
    assert {repo["id"] for repo in apps} == {"finance-report", "truealpha"}
    assert {repo["governance"] for repo in apps} == {"autonomous"}


def test_missing_checkout_is_warning_but_manifest_remains_valid(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    app = tmp_path / "repos" / "app"
    (app / "AGENTS.md").unlink()
    app.rmdir()

    result = validate_manifest(tmp_path, manifest)

    assert result.ok
    assert [warning.code for warning in result.warnings] == ["checkout-missing"]


def test_missing_checkout_does_not_skip_authority_schema(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    app = manifest["repositories"][1]
    app["path"] = "repos/missing"
    app["authority"] = []

    result = validate_manifest(tmp_path, manifest)

    assert {error.code for error in result.errors} >= {"authority"}
    assert {warning.code for warning in result.warnings} == {"checkout-missing"}


def test_duplicate_and_escaping_paths_fail(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    duplicate = dict(manifest["repositories"][0])
    duplicate["path"] = "../outside"
    manifest["repositories"].append(duplicate)

    result = validate_manifest(tmp_path, manifest)

    assert not result.ok
    assert {error.code for error in result.errors} >= {
        "duplicate-id",
        "repository-path",
    }


def test_checkout_kind_must_match_path(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    manifest["repositories"][0]["path"] = "repos/app"
    manifest["repositories"][1]["path"] = "."

    result = validate_manifest(tmp_path, manifest)

    assert {error.code for error in result.errors} >= {
        "root-checkout",
        "submodule-checkout",
    }


def test_workspace_requires_id_one_root_and_unique_focus(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    manifest["workspace"]["id"] = ""
    manifest["workspace"]["focus"].append("infra2")
    manifest["repositories"][0]["checkout"] = "submodule"

    result = validate_manifest(tmp_path, manifest)

    assert {error.code for error in result.errors} >= {
        "workspace-id",
        "root-count",
        "focus-duplicate",
    }


def test_external_application_cannot_be_coordinated_or_focused(
    tmp_path: Path,
) -> None:
    manifest = _workspace(tmp_path)
    app = manifest["repositories"][1]
    app["governance"] = "coordinated"
    manifest["workspace"]["focus"].append("app")

    result = validate_manifest(tmp_path, manifest)

    assert {error.code for error in result.errors} >= {
        "governance-role",
        "focus-autonomy",
    }


def test_missing_authority_and_invalid_preferences_fail(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    manifest["workspace"]["preferences"] = ["missing.md"]
    manifest["repositories"][0]["authority"] = ["missing.md"]

    result = validate_manifest(tmp_path, manifest)

    assert {error.code for error in result.errors} >= {
        "preference-path",
        "authority-path",
    }


def test_load_manifest_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "repos.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    try:
        load_manifest(path)
    except HarnessManifestError as exc:
        assert "YAML mapping" in str(exc)
    else:
        raise AssertionError("expected HarnessManifestError")


def test_check_workspace_and_cli_json_report_manifest_errors(
    tmp_path: Path, capsys
) -> None:
    manifest_path = tmp_path / "broken.yaml"
    manifest_path.write_text("repositories: [", encoding="utf-8")

    result = check_workspace(tmp_path, manifest_path)
    exit_code = main(
        ["check", "--root", str(tmp_path), "--manifest", str(manifest_path), "--json"]
    )
    output = json.loads(capsys.readouterr().out)

    assert not result.ok
    assert exit_code == 1
    assert output["errors"][0]["code"] == "manifest-read"


def test_manifest_round_trip_fixture_is_yaml(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    path = tmp_path / "repos.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    result = check_workspace(tmp_path, path)

    assert result.ok


def test_cli_resolves_relative_manifest_from_requested_root(
    tmp_path: Path, capsys
) -> None:
    manifest = _workspace(tmp_path)
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    (harness_dir / "repos.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    exit_code = main(
        ["check", "--root", str(tmp_path), "--manifest", "harness/repos.yaml"]
    )

    assert exit_code == 0
    assert "harness check: PASS" in capsys.readouterr().out
