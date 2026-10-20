"""Mutation-verified tests for #1123 config_hash and deploy_guard_audit blind spots.

Covers the 7 required mutation scenarios (1123-R4 / O1):
  1. Wildcard COPY (`COPY pyproject.t* /tmp/`) and multi-line `\\` continuations.
  2. `${VAR:-default}` and `${VAR-default}` in build context and bind-mount sources.
  3. `dockerfile:` pointing to a missing file raises FileNotFoundError / fails audit.
  4. Renaming `platform/12.alerting` to `platform/12_alerting` (unmapped compose + floor drop).
  5. Truncating `_SERVICE_ROOTS = ('bootstrap',)` fails audit on missing roots and floors.
  6. Rewriting `./` bind mounts and `context:` to bare `${VAR}` fails audit on input floor and Dockerfile canary.
  7. `main()` prints per-service-root compose and input counts on stdout.
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from common.deploy import config_hash, deploy_guard_audit
from common.deploy.dependencies import fanout_coverage_violations


def _make_service_tree(
    root: Path,
    compose_text: str,
    dockerfile_text: str | None = None,
    extra_files: tuple[str, ...] = (),
) -> Path:
    service_dir = root / "platform" / "99.ghost"
    service_dir.mkdir(parents=True, exist_ok=True)
    compose = service_dir / "compose.yaml"
    compose.write_text(compose_text, encoding="utf-8")
    if dockerfile_text is not None:
        (service_dir / "Dockerfile").write_text(dockerfile_text, encoding="utf-8")
    for rel in extra_files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("payload\n", encoding="utf-8")
    return compose


def test_wildcard_copy_and_backslash_continuation_resolve_and_fail_on_unmatched_glob(
    tmp_path: Path,
) -> None:
    """Case 1: Wildcard COPY (`COPY pyproject.t* /tmp/`) and `\\` continuation resolve real files,
    flag undeclared repo-root fan-out violations, and fail closed when a glob matches 0 files."""
    compose = _make_service_tree(
        tmp_path,
        "services:\n"
        "  ghost:\n"
        "    build:\n"
        "      context: ../..\n"
        "      dockerfile: platform/99.ghost/Dockerfile\n",
        "FROM python:3.11-slim\n"
        "COPY \\\n"
        "  # comment inside continuation\n"
        "  pyproject.t* \\\n"
        "  common/a?.py \\\n"
        "  /tmp/\n",
        extra_files=("pyproject.toml", "common/a1.py"),
    )
    artifacts = config_hash._compose_artifact_files(
        str(compose), compose.read_text(encoding="utf-8")
    )
    assert (tmp_path / "pyproject.toml").resolve() in artifacts
    assert (tmp_path / "common/a1.py").resolve() in artifacts

    violations = fanout_coverage_violations([compose], manifest={}, root=tmp_path)
    assert violations == [
        "platform/ghost: common/a1.py",
        "platform/ghost: pyproject.toml",
    ]

    # Unmatched glob must fail closed rather than silently returning [].
    (tmp_path / "platform/99.ghost/Dockerfile").write_text(
        "FROM python:3.11-slim\nCOPY nonexistent_*.toml /tmp/\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="nonexistent_\\*\\.toml"):
        config_hash._compose_artifact_files(
            str(compose), compose.read_text(encoding="utf-8")
        )


def test_env_substituted_context_and_bind_mount_with_defaults_resolve_inputs(
    tmp_path: Path,
) -> None:
    """Case 2: `${VAR:-default}` and `${VAR-default}` in build context, dockerfile, and bind mounts
    resolve the default path and flag undeclared fan-out inputs."""
    compose = _make_service_tree(
        tmp_path,
        "services:\n"
        "  ghost:\n"
        "    build:\n"
        "      context: ${BUILD_CTX:-../..}\n"
        "      dockerfile: ${DOCKERFILE_PATH-platform/99.ghost/Dockerfile}\n"
        "    volumes:\n"
        "      - ${CONF_PATH:-../../common/shared.yaml}:/etc/shared.yaml:ro\n"
        "      - type: bind\n"
        "        source: ${LOCAL_PATH-./local.yaml}\n"
        "        target: /etc/local.yaml\n",
        "FROM python:3.11-slim\nCOPY ${LOCK_FILE:-uv.lock} /tmp/uv.lock\n",
        extra_files=("uv.lock", "common/shared.yaml", "platform/99.ghost/local.yaml"),
    )
    artifacts = config_hash._compose_artifact_files(
        str(compose), compose.read_text(encoding="utf-8")
    )
    assert (tmp_path / "uv.lock").resolve() in artifacts
    assert (tmp_path / "common/shared.yaml").resolve() in artifacts
    assert (tmp_path / "platform/99.ghost/local.yaml").resolve() in artifacts

    violations = fanout_coverage_violations([compose], manifest={}, root=tmp_path)
    assert violations == [
        "platform/ghost: common/shared.yaml",
        "platform/ghost: uv.lock",
    ]


def test_missing_explicit_dockerfile_raises_and_fails_audit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Case 3: A `dockerfile:` that points to a missing file raises FileNotFoundError and makes
    both `audit()` and `main()` fail closed."""
    compose = _make_service_tree(
        tmp_path,
        "services:\n"
        "  ghost:\n"
        "    build:\n"
        "      context: .\n"
        "      dockerfile: Dockerfile.missing\n",
    )
    with pytest.raises(FileNotFoundError, match="Dockerfile.missing"):
        config_hash._compose_artifact_files(
            str(compose), compose.read_text(encoding="utf-8")
        )

    violations = deploy_guard_audit.audit(root=tmp_path)
    assert any("Dockerfile.missing" in v for v in violations)
    assert deploy_guard_audit.main(root=tmp_path) == 1
    assert "Dockerfile.missing" in capsys.readouterr().out


def test_renaming_service_dir_without_dot_fails_unmapped_and_floor_guards(
    tmp_path: Path,
) -> None:
    """Case 4: Renaming `platform/12.alerting` to `platform/12_alerting` is caught by both
    `find_unmapped_composes` and the `platform` per-root compose/input floor."""
    alerting_dir = tmp_path / "platform" / "12_alerting"
    alerting_dir.mkdir(parents=True)
    (alerting_dir / "compose.yaml").write_text(
        "services:\n  alerting:\n    image: alpine:3.19\n",
        encoding="utf-8",
    )
    unmapped = deploy_guard_audit.find_unmapped_composes(root=tmp_path)
    assert unmapped == ["platform/12_alerting/compose.yaml"]

    violations = deploy_guard_audit.floor_violations([], root=tmp_path)
    assert (
        "platform/12_alerting/compose.yaml: compose file does not map to a service key in service_key_from_path"
        in violations
    )
    assert any(
        v.startswith("platform: found 0 compose files, below floor 13")
        for v in violations
    )
    assert any(
        v.startswith("platform: read 0 config-hash inputs, below floor 350")
        for v in violations
    )


def test_truncating_service_roots_fails_audit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Case 5: Setting `_SERVICE_ROOTS = ('bootstrap',)` fails `audit()` and `main()`."""
    monkeypatch.setattr(deploy_guard_audit, "_SERVICE_ROOTS", ("bootstrap",))
    violations = deploy_guard_audit.audit()
    assert any(
        "_SERVICE_ROOTS is missing required service roots" in v for v in violations
    )
    assert any(
        v.startswith("platform: found 0 compose files, below floor") for v in violations
    )
    assert deploy_guard_audit.main() == 1
    out = capsys.readouterr().out
    assert "_SERVICE_ROOTS is missing required service roots" in out


def test_rewriting_mounts_and_contexts_to_bare_var_fails_dockerfile_canary_and_floors(
    tmp_path: Path,
) -> None:
    """Case 6: Rewriting `./` bind mounts and `context:` to bare `${VAR}` (no default) drops inputs
    to 0 and triggers both `dockerfile_canary_violations` and per-root input floor failures."""
    for sr in ("platform", "finance_report", "truealpha", "bootstrap"):
        shutil.copytree(deploy_guard_audit.ROOT / sr, tmp_path / sr)

    for compose in deploy_guard_audit.find_service_composes(root=tmp_path):
        text = compose.read_text(encoding="utf-8")
        mutated = (
            text.replace("context: ../..", "context: ${UNSET_CTX}")
            .replace("context: .", "context: ${UNSET_CTX}")
            .replace("build: .", "build: ${UNSET_CTX}")
            .replace("- ./", "- ${UNSET_MOUNT}/")
        )
        compose.write_text(mutated, encoding="utf-8")

    composes = deploy_guard_audit.find_service_composes(root=tmp_path)
    canary_hits = deploy_guard_audit.dockerfile_canary_violations(
        composes, root=tmp_path
    )
    assert (
        "platform/12.alerting: has Dockerfile but config hash reads 0 inputs"
        in canary_hits
    )
    assert (
        "bootstrap/06.iac_runner: has Dockerfile but config hash reads 0 inputs"
        in canary_hits
    )

    violations = deploy_guard_audit.floor_violations(composes, root=tmp_path)
    assert any(
        v.startswith("platform: read 0 config-hash inputs, below floor 350")
        for v in violations
    )
    assert deploy_guard_audit.main(root=tmp_path) == 1


def test_audit_cli_includes_vault_agent_template_and_prints_per_root_breakdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Case 7: `find_service_composes()` includes `bootstrap/templates/vault-agent.compose.yaml`
    and `preview/compose.yaml`, and `main()` exits 0 while printing each service root's counts."""
    composes = deploy_guard_audit.find_service_composes()
    rels = {c.relative_to(deploy_guard_audit.ROOT).as_posix() for c in composes}
    assert "bootstrap/templates/vault-agent.compose.yaml" in rels
    assert "finance_report/finance_report/preview/compose.yaml" in rels
    assert "truealpha/truealpha/preview/compose.yaml" in rels
    assert deploy_guard_audit.find_unmapped_composes() == []

    assert deploy_guard_audit.main() == 0
    out = capsys.readouterr().out
    for sr in ("platform", "finance_report", "truealpha", "bootstrap"):
        assert f"  {sr}: " in out
        assert "compose files," in out
        assert "inputs" in out
