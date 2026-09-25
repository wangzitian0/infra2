"""Proof that infra2 consumes the released SDK rather than redefining shared contracts."""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages
from infra2_sdk.delivery import DisagreementKind, detect_disagreement

ROOT = Path(__file__).resolve().parents[2]
LOCAL_STAGES = ROOT / "docs/ssot/delivery-stages.yaml"
OPS_CHECKS = ROOT / ".github/workflows/ops-checks.yml"


def test_infra_pins_the_expected_sdk_release() -> None:
    assert version("infra2-sdk") == "2.3.0"


def test_alerting_dockerfile_sdk_pin_matches_pyproject() -> None:
    dockerfile = (ROOT / "platform/12.alerting/Dockerfile").read_text(encoding="utf-8")
    pyproject_data = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = pyproject_data.get("project", {}).get("dependencies", [])
    sdk_entries = [dep for dep in dependencies if dep.startswith("infra2-sdk @ ")]
    assert len(sdk_entries) == 1, (
        f"Expected exactly 1 infra2-sdk requirement in pyproject.toml, found: {sdk_entries}"
    )
    sdk_requirement = sdk_entries[0]
    assert sdk_requirement in dockerfile, (
        f"Alerting Dockerfile must contain the exact infra2-sdk requirement from pyproject.toml:\n"
        f"Wanted: {sdk_requirement}\n"
        f"In Dockerfile: {dockerfile}"
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

    assert 'value.startswith("infra2-sdk @ "' in install_command
    assert (
        'python -m pip install httpx python-dotenv rich "$sdk_requirement"'
        in install_command
    )
    for event in ("push", "pull_request"):
        paths = workflow["on"][event]["paths"]
        # The installed SDK is the wheel pinned in pyproject.toml/uv.lock — moving
        # the repos/infra2-sdk submodule pointer never changes it, so that path was
        # a no-op trigger and was removed (#506).
        assert {"pyproject.toml", "uv.lock"} <= set(paths)
        assert "repos/infra2-sdk" not in paths


def test_reserved_deploy_canary_slot_is_globally_serialized() -> None:
    workflow = yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8"))
    concurrency = workflow["jobs"]["deploy-v2-canary"]["concurrency"]

    assert concurrency == {
        "group": "deploy-v2-canary-pr-999",
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
    assert detect_disagreement(disagreement_results) == DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE

    producers = {
        DisagreementKind.NONE: lambda: detect_disagreement(normal_results),
        DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE: lambda: detect_disagreement(disagreement_results),
    }
    produced_kinds = {kind: fn() for kind, fn in producers.items()}
    for kind, val in produced_kinds.items():
        assert val == kind

    assert set(producers.keys()) == set(DisagreementKind), (
        f"Missing execution test for DisagreementKind: {set(DisagreementKind) - set(producers.keys())}"
    )
