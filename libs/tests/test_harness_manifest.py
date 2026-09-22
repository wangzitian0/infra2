from __future__ import annotations

import json
from pathlib import Path
import subprocess

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
    (app / ".git").write_text("gitdir: fixture", encoding="utf-8")
    (app / "AGENTS.md").write_text("authority", encoding="utf-8")
    return _valid_manifest()


def test_committed_inventory_is_valid_and_apps_are_autonomous() -> None:
    manifest = load_manifest(ROOT / "harness" / "repos.yaml")
    # submodules_expected=False: this test must be portable across a machine with
    # submodules initialized (a developer's checkout) and one without (CI, and any
    # fresh non-recursive clone) — same call the harness-check CI job makes. The
    # declarative correctness of harness/repos.yaml doesn't depend on which one
    # happens to be running the test (#506).
    result = validate_manifest(ROOT, manifest, submodules_expected=False)

    assert result.ok, result.to_dict()
    assert result.repository_count == 5
    assert manifest["workspace"]["focus"] == ["infra2", "infra2-sdk"]
    tooling = next(
        repo for repo in manifest["repositories"] if repo["id"] == "oh-my-code-agent"
    )
    assert tooling["path"] == "oh-my-code-agent"
    assert tooling["role"] == "workspace-tooling"
    assert tooling["governance"] == "coordinated"
    apps = [
        repo
        for repo in manifest["repositories"]
        if repo["role"] == "external-application"
    ]
    assert {repo["id"] for repo in apps} == {"finance-report", "truealpha"}
    assert {repo["governance"] for repo in apps} == {"autonomous"}

    # dev_env #51: infra2-sdk is a nested submodule that does not carry a
    # Repo-layer rules projection — it must be declared, not left implicit, so
    # the A-layer discovery matrix in dev_env can assert "expected no Repo
    # layer" from this manifest instead of guessing.
    sdk = next(
        repo for repo in manifest["repositories"] if repo["id"] == "infra2-sdk"
    )
    assert sdk.get("rules_layer") == "none"


def test_missing_checkout_is_error_by_default(tmp_path: Path) -> None:
    """Non-optional (the default) means an uninitialized checkout is a real gap,
    not a warning nobody reads — #506."""
    manifest = _workspace(tmp_path)
    app = tmp_path / "repos" / "app"
    (app / "AGENTS.md").unlink()
    (app / ".git").unlink()
    app.rmdir()

    result = validate_manifest(tmp_path, manifest)

    assert not result.ok
    assert [error.code for error in result.errors] == ["checkout-missing"]


def test_missing_checkout_is_warning_when_declared_optional(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    manifest["repositories"][1]["optional"] = True
    app = tmp_path / "repos" / "app"
    (app / "AGENTS.md").unlink()
    (app / ".git").unlink()
    app.rmdir()

    result = validate_manifest(tmp_path, manifest)

    assert result.ok
    assert [warning.code for warning in result.warnings] == ["checkout-missing"]


def test_empty_submodule_directory_is_uninitialized_error_by_default(
    tmp_path: Path,
) -> None:
    manifest = _workspace(tmp_path)
    app = tmp_path / "repos" / "app"
    (app / "AGENTS.md").unlink()
    (app / ".git").unlink()

    result = validate_manifest(tmp_path, manifest)

    assert not result.ok
    assert [error.code for error in result.errors] == ["checkout-uninitialized"]


def test_empty_submodule_directory_is_warning_when_declared_optional(
    tmp_path: Path,
) -> None:
    manifest = _workspace(tmp_path)
    manifest["repositories"][1]["optional"] = True
    app = tmp_path / "repos" / "app"
    (app / "AGENTS.md").unlink()
    (app / ".git").unlink()

    result = validate_manifest(tmp_path, manifest)

    assert result.ok
    assert [warning.code for warning in result.warnings] == ["checkout-uninitialized"]


def test_submodules_expected_false_downgrades_to_warning_even_when_not_optional(
    tmp_path: Path,
) -> None:
    """CI never runs `submodules: true` — every submodule path is absent by
    design there, not drift. submodules_expected=False must not error on that,
    even for a non-optional repo (#506)."""
    manifest = _workspace(tmp_path)
    app = tmp_path / "repos" / "app"
    (app / "AGENTS.md").unlink()
    (app / ".git").unlink()
    app.rmdir()

    result = validate_manifest(tmp_path, manifest, submodules_expected=False)

    assert result.ok
    assert [warning.code for warning in result.warnings] == ["checkout-missing"]


def test_submodules_expected_false_does_not_affect_the_root_checkout(
    tmp_path: Path,
) -> None:
    """The flag is scoped to `checkout: submodule` — a genuinely broken root
    checkout (this repo itself) must still error."""
    manifest = _workspace(tmp_path)
    (tmp_path / "AGENTS.md").unlink()

    result = validate_manifest(tmp_path, manifest, submodules_expected=False)

    assert not result.ok
    assert any(error.code == "authority-path" for error in result.errors)


def test_missing_checkout_does_not_skip_authority_schema(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    app = manifest["repositories"][1]
    app["path"] = "repos/missing"
    app["authority"] = []
    app["optional"] = True

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


def test_unsupported_rules_layer_value_fails(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    app = next(repo for repo in manifest["repositories"] if repo["id"] == "app")
    app["rules_layer"] = "sometimes"

    result = validate_manifest(tmp_path, manifest)

    assert not result.ok
    assert [error.code for error in result.errors] == ["rules-layer"]


def test_non_string_rules_layer_value_fails_without_raising(tmp_path: Path) -> None:
    """A YAML sequence/map value must become a validation finding, not an
    uncaught TypeError from the `not in ALLOWED_RULES_LAYER` set membership
    test on an unhashable value."""
    manifest = _workspace(tmp_path)
    app = next(repo for repo in manifest["repositories"] if repo["id"] == "app")
    app["rules_layer"] = ["none"]

    result = validate_manifest(tmp_path, manifest)

    assert not result.ok
    assert [error.code for error in result.errors] == ["rules-layer"]


def test_rules_layer_none_is_accepted(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    app = next(repo for repo in manifest["repositories"] if repo["id"] == "app")
    app["rules_layer"] = "none"

    result = validate_manifest(tmp_path, manifest)

    assert result.ok, result.to_dict()


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


def test_contract_tiering_schema_validation(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    app = manifest["repositories"][1]
    app["contract"] = "floating"

    result = validate_manifest(tmp_path, manifest)
    assert not result.ok
    assert any(error.code == "contract" for error in result.errors)

    app["contract"] = ["pinned"]
    result = validate_manifest(tmp_path, manifest)
    assert not result.ok
    assert any(error.code == "contract" for error in result.errors)

    app["contract"] = "pinned"
    result = validate_manifest(tmp_path, manifest)
    assert not any(error.code == "contract" for error in result.errors)

    app["contract"] = "snapshot"
    result = validate_manifest(tmp_path, manifest)
    assert not any(error.code == "contract" for error in result.errors)


def test_submodule_pin_drift_distinguishes_pinned_vs_snapshot(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    parent_pin_sha = "1" * 40
    head_sha = "2" * 40

    def runner(argv, **_kwargs):
        args = argv[3:]
        if args == ["ls-tree", "HEAD", "--", "repos/app"]:
            return subprocess.CompletedProcess(
                argv, 0, f"160000 commit {parent_pin_sha}\trepos/app\n", ""
            )
        if args == ["rev-parse", "HEAD^{commit}"]:
            return subprocess.CompletedProcess(argv, 0, f"{head_sha}\n", "")
        raise AssertionError(f"unexpected git call: {argv}")

    # Case 1: contract == "pinned" with drift -> error with code="pin-drift", ok is False
    manifest["repositories"][1]["contract"] = "pinned"
    pinned_result = validate_manifest(tmp_path, manifest, runner=runner)
    assert not pinned_result.ok
    drift_errors = [e for e in pinned_result.errors if e.code == "pin-drift"]
    assert len(drift_errors) == 1
    assert "pinned submodule has drifted" in drift_errors[0].message
    assert f"checkout={head_sha[:12]}" in drift_errors[0].message
    assert f"parent={parent_pin_sha[:12]}" in drift_errors[0].message

    # Case 2: contract == "snapshot" with drift -> warning with code="pin-drift", ok is True
    manifest["repositories"][1]["contract"] = "snapshot"
    snapshot_result = validate_manifest(tmp_path, manifest, runner=runner)
    assert snapshot_result.ok is True
    drift_warnings = [w for w in snapshot_result.warnings if w.code == "pin-drift"]
    assert len(drift_warnings) == 1
    assert "snapshot submodule has advanced" in drift_warnings[0].message
    assert f"checkout={head_sha[:12]}" in drift_warnings[0].message
    assert f"parent={parent_pin_sha[:12]}" in drift_warnings[0].message


def test_committed_inventory_specifies_contract_tiering() -> None:
    manifest = load_manifest(ROOT / "harness" / "repos.yaml")
    expected = {
        "infra2": "pinned",
        "infra2-sdk": "pinned",
        "oh-my-code-agent": "snapshot",
        "finance-report": "snapshot",
        "truealpha": "snapshot",
    }
    actual = {repo["id"]: repo.get("contract") for repo in manifest["repositories"]}
    assert actual == expected

