"""Proof that infra2 consumes the released SDK rather than redefining shared contracts."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages

ROOT = Path(__file__).resolve().parents[2]
LOCAL_STAGES = ROOT / "docs/ssot/delivery-stages.yaml"


def test_infra_pins_the_expected_sdk_release() -> None:
    assert version("infra2-sdk") == "0.1.0"


def test_local_stage_mirror_matches_the_released_sdk() -> None:
    document = yaml.safe_load(LOCAL_STAGES.read_text(encoding="utf-8"))
    assert str(document["sdk_version"]) == version("infra2-sdk")
    assert load_delivery_stages(LOCAL_STAGES) == load_delivery_stages()


def test_retired_compatibility_modules_stay_removed() -> None:
    assert not (ROOT / "libs/ci_gate_schema.py").exists()
    assert not (ROOT / "libs/pipeline_stage_contract.py").exists()
