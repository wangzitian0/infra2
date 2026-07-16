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
        assert {"pyproject.toml", "uv.lock", "repos/infra2-sdk"} <= set(paths)


def test_retired_compatibility_modules_stay_removed() -> None:
    assert not (ROOT / "libs/ci_gate_schema.py").exists()
    assert not (ROOT / "libs/pipeline_stage_contract.py").exists()
