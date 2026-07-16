"""Proof that infra2 consumes the released SDK rather than redefining shared contracts."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages

ROOT = Path(__file__).resolve().parents[2]
LOCAL_STAGES = ROOT / "docs/ssot/delivery-stages.yaml"
OPS_CHECKS = ROOT / ".github/workflows/ops-checks.yml"


def test_infra_pins_the_expected_sdk_release() -> None:
    assert version("infra2-sdk") == "0.3.0"


def test_local_stage_mirror_matches_the_released_sdk() -> None:
    document = yaml.safe_load(LOCAL_STAGES.read_text(encoding="utf-8"))
    local_stages = load_delivery_stages(LOCAL_STAGES)
    released_stages = load_delivery_stages()

    assert str(document["sdk_version"]) == version("infra2-sdk")
    assert local_stages == released_stages


def test_deploy_canary_installs_the_declared_sdk_requirement() -> None:
    workflow = OPS_CHECKS.read_text(encoding="utf-8")
    deploy_canary_job = workflow.split("  deploy-v2-canary:", 1)[1].split(
        "  preview-leak-check:", 1
    )[0]

    assert 'value.startswith("infra2-sdk @ "' in deploy_canary_job
    assert (
        'python -m pip install httpx python-dotenv rich "$sdk_requirement"'
        in deploy_canary_job
    )
    for sdk_pin_surface in ('"pyproject.toml"', '"uv.lock"', '"repos/infra2-sdk"'):
        assert workflow.count(sdk_pin_surface) >= 2


def test_retired_compatibility_modules_stay_removed() -> None:
    assert not (ROOT / "libs/ci_gate_schema.py").exists()
    assert not (ROOT / "libs/pipeline_stage_contract.py").exists()
