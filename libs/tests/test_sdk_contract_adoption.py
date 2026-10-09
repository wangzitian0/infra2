"""Proof that infra2 consumes the released SDK rather than redefining shared contracts."""

from __future__ import annotations

import ast
import subprocess
from importlib.metadata import version
from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages
from infra2_sdk.delivery import DisagreementKind, detect_disagreement

ROOT = Path(__file__).resolve().parents[2]
LOCAL_STAGES = ROOT / "docs/ssot/delivery-stages.yaml"
OPS_CHECKS = ROOT / ".github/workflows/ops-checks.yml"


DEPRECATED_SDK_MODULES = {
    "images": "infra2_sdk.images",
    "deploy_health": "infra2_sdk.deploy_health (use infra2_sdk.deploy)",
    "dispatch": "infra2_sdk.dispatch (use infra2_sdk.deploy)",
    "release": "infra2_sdk.release (use infra2_sdk.refs)",
}

DEPRECATED_RUNTIME_MODULES = {
    "environ": "infra2_sdk.runtime.environ (use infra2_sdk.runtime.environment)",
    "dependencies": "infra2_sdk.runtime.dependencies (use infra2_sdk.runtime.health)",
    "probes": "infra2_sdk.runtime.probes (use infra2_sdk.runtime.health)",
}


def _sdk_contract_violations(source: str) -> list[str]:
    """Imports of an ``infra2_sdk`` private name or deprecated surface in ``source``.

    Only the published contract is stable across SDK releases (#955): a module or name
    starting with an underscore is private, ``infra2_sdk.images`` /
    ``to_otel_resource_attributes`` are removed in 3.0.0, and secondary module
    compatibility shims (deploy_health, dispatch, release, runtime.probes, etc.)
    are deprecated in favor of canonical modules (#1136).
    """
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [(alias.name, ()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [(node.module, tuple(alias.name for alias in node.names))]
        else:
            if isinstance(node, ast.Attribute) and (
                node.attr == "to_otel_resource_attributes"
            ):
                violations.append(f"deprecated attribute {node.attr}")
            continue
        for module, names in modules:
            parts = module.split(".")
            if parts[0] != "infra2_sdk":
                continue
            if any(part.startswith("_") for part in parts[1:]):
                violations.append(f"private module {module}")
            for shim_name, shim_desc in DEPRECATED_SDK_MODULES.items():
                if parts[1:2] == [shim_name] or (
                    len(parts) == 1 and shim_name in names
                ):
                    violations.append(f"deprecated module {shim_desc}")
            if len(parts) >= 2 and parts[1] == "runtime":
                for shim_name, shim_desc in DEPRECATED_RUNTIME_MODULES.items():
                    if parts[2:3] == [shim_name] or (
                        len(parts) == 2 and shim_name in names
                    ):
                        violations.append(f"deprecated module {shim_desc}")
            violations.extend(
                f"private name {module}.{name}"
                for name in names
                if name.startswith("_")
            )
    return violations


def _infra_python_sources() -> list[Path]:
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    return [
        ROOT / name
        for name in tracked
        if name and not name.startswith("repos/") and (ROOT / name).is_file()
    ]


def test_infra_imports_only_the_published_sdk_contract() -> None:
    sources = _infra_python_sources()
    assert len(sources) > 100, "the tracked-file scan found almost nothing"
    found = {
        str(path.relative_to(ROOT)): violations
        for path in sources
        if (violations := _sdk_contract_violations(path.read_text(encoding="utf-8")))
    }
    assert found == {}, f"private or deprecated infra2_sdk use: {found}"


def test_the_sdk_contract_scan_flags_private_and_deprecated_use() -> None:
    assert _sdk_contract_violations(
        "from infra2_sdk.refs import _ls_remote_rows, classify_ref\n"
    ) == ["private name infra2_sdk.refs._ls_remote_rows"]
    assert _sdk_contract_violations("import infra2_sdk._transport\n") == [
        "private module infra2_sdk._transport"
    ]
    assert _sdk_contract_violations("from infra2_sdk._transport import send\n") == [
        "private module infra2_sdk._transport"
    ]
    assert _sdk_contract_violations("from infra2_sdk import images\n") == [
        "deprecated module infra2_sdk.images"
    ]
    assert _sdk_contract_violations("import infra2_sdk.images\n") == [
        "deprecated module infra2_sdk.images"
    ]
    assert _sdk_contract_violations("identity.to_otel_resource_attributes()\n") == [
        "deprecated attribute to_otel_resource_attributes"
    ]
    assert _sdk_contract_violations(
        "from infra2_sdk.deploy_health import poll_until_healthy\n"
    ) == ["deprecated module infra2_sdk.deploy_health (use infra2_sdk.deploy)"]
    assert _sdk_contract_violations(
        "from infra2_sdk.release import ReleaseError\n"
    ) == ["deprecated module infra2_sdk.release (use infra2_sdk.refs)"]
    assert _sdk_contract_violations(
        "from infra2_sdk.runtime.probes import DependencyStatus\n"
    ) == ["deprecated module infra2_sdk.runtime.probes (use infra2_sdk.runtime.health)"]
    assert (
        _sdk_contract_violations(
            "from infra2_sdk.refs import ls_remote_rows, redact_repo\n"
            "from other_pkg._private import _x\n"
        )
        == []
    )


def test_local_stage_mirror_matches_the_released_sdk() -> None:
    document = yaml.safe_load(LOCAL_STAGES.read_text(encoding="utf-8"))
    local_stages = load_delivery_stages(LOCAL_STAGES)
    released_stages = load_delivery_stages()

    assert str(document["sdk_version"]) == version("infra2-sdk")
    assert local_stages == released_stages


def test_deploy_canary_installs_the_declared_sdk_requirement() -> None:
    workflow = yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8"))
    job = workflow["jobs"]["deploy-v2-canary"]
    steps = {step["name"]: step for step in job["steps"]}
    install_command = steps["Install runtime dependencies"]["run"]

    assert 'sdk_requirement="$(python tools/sdk_requirement.py)"' in install_command
    assert (
        'python -m pip install httpx python-dotenv rich "$sdk_requirement"'
        in install_command
    )
    for event in ("push", "pull_request"):
        paths = workflow["on"][event]["paths"]
        # The installed SDK is the wheel uv.lock pins, read by tools/sdk_requirement.py
        # (#1115). Moving the repos/infra2-sdk submodule pointer never changes it, so
        # that path was a no-op trigger and was removed (#506).
        assert {"pyproject.toml", "uv.lock", "tools/sdk_requirement.py"} <= set(paths)
        assert "repos/infra2-sdk" not in paths


def test_reserved_deploy_canary_slot_is_globally_serialized() -> None:
    workflow = yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8"))
    concurrency = workflow["jobs"]["deploy-v2-canary"]["concurrency"]

    assert concurrency == {
        "group": "deploy-v2-canary",
        "cancel-in-progress": False,
    }


def test_pull_request_canary_binds_exact_head_authority_to_cloneable_branch() -> None:
    workflow = yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8"))
    job = workflow["jobs"]["deploy-v2-canary"]
    steps = {step["name"]: step for step in job["steps"]}
    env = steps["Run deploy_v2 canary"]["env"]
    iac_ref = env["IAC_REF_INPUT"]
    clone_ref = env["IAC_CLONE_REF_INPUT"]

    assert "github.event_name == 'pull_request'" in iac_ref
    assert "github.event.pull_request.head.sha" in iac_ref
    assert iac_ref.endswith("|| 'main' }}")
    assert "github.head_ref" in clone_ref
    assert clone_ref.endswith("|| '' }}")

    run_command = steps["Run deploy_v2 canary"]["run"]
    assert 'clone_args+=(--iac-clone-ref "$IAC_CLONE_REF_INPUT")' in run_command
    assert '"${clone_args[@]}"' in run_command


def test_retired_compatibility_modules_stay_removed() -> None:
    assert not (ROOT / "libs/ci_gate_schema.py").exists()
    assert not (ROOT / "libs/pipeline_stage_contract.py").exists()
    assert not (ROOT / "libs/image_pins.py").exists()
    assert not (ROOT / "tools/lint_platform_image_pins.py").exists()


def test_detect_disagreement_can_produce_every_disagreement_kind() -> None:
    """Execute detect_disagreement with real StageResult inputs for all DisagreementKind enum values.

    Anti-Tautology (Rule 7): tests real execution outcomes, never string-matching inspect.getsource.
    """
    from infra2_sdk.delivery import (
        FailureDomain,
        PipelineStage,
        StageStatus,
        make_stage_result,
    )

    normal_results = [
        make_stage_result(
            source="ci",
            environment="staging",
            stage=PipelineStage.WATCHDOG,
            target="app",
            status=StageStatus.PASS,
        )
    ]
    assert detect_disagreement(normal_results) == DisagreementKind.NONE

    disagreement_results = [
        make_stage_result(
            source="ci",
            environment="staging",
            stage=PipelineStage.WATCHDOG,
            target="app",
            status=StageStatus.PASS,
        ),
        make_stage_result(
            source="ci",
            environment="staging",
            stage=PipelineStage.DEPLOY_SMOKE,
            target="app",
            status=StageStatus.FAIL,
            failure_domain=FailureDomain.TRAEFIK_PUBLIC_ROUTE,
        ),
    ]
    assert (
        detect_disagreement(disagreement_results)
        == DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE
    )

    producers = {
        DisagreementKind.NONE: lambda: detect_disagreement(normal_results),
        DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE: lambda: detect_disagreement(
            disagreement_results
        ),
    }
    produced_kinds = {kind: fn() for kind, fn in producers.items()}
    for kind, val in produced_kinds.items():
        assert val == kind

    assert set(producers.keys()) == set(DisagreementKind), (
        f"Missing execution test for DisagreementKind: {set(DisagreementKind) - set(producers.keys())}"
    )
