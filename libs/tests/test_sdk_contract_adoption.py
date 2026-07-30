"""Proof that infra2 consumes the released SDK rather than redefining shared contracts."""

from __future__ import annotations

import inspect
from importlib.metadata import version
from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages
from infra2_sdk.delivery import DisagreementKind, detect_disagreement

ROOT = Path(__file__).resolve().parents[2]
LOCAL_STAGES = ROOT / "docs/ssot/delivery-stages.yaml"
OPS_CHECKS = ROOT / ".github/workflows/ops-checks.yml"


def test_infra_pins_the_expected_sdk_release() -> None:
    assert version("infra2-sdk") == "1.0.0"


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


def test_retired_compatibility_modules_stay_removed() -> None:
    assert not (ROOT / "libs/ci_gate_schema.py").exists()
    assert not (ROOT / "libs/pipeline_stage_contract.py").exists()


def test_detect_disagreement_can_produce_every_disagreement_kind() -> None:
    """A DisagreementKind the SDK declares but detect_disagreement() can never actually
    return is a live-looking capability nobody built — exactly the shape #543's route-canary
    retirement left behind (deleted the only producer, never revisited the SDK enum). infra2-sdk
    v1.0.0 removed the two values that were unreachable here (CANARY_APP_READINESS,
    HEARTBEAT_PROBE_RESULT), so this now scans detect_disagreement()'s own source for which
    members it references with no exemptions: every declared value must be reachable, or a
    NEW unreachable value fails here immediately instead of requiring a fresh manual audit
    across every consumer repo to notice."""
    source = inspect.getsource(detect_disagreement)
    reachable = {
        member
        for member in DisagreementKind
        if f"DisagreementKind.{member.name}" in source
    }
    # NONE has an explicit `return DisagreementKind.NONE` fallthrough — a real, reachable
    # return value like the rest.
    assert reachable == set(DisagreementKind), (
        f"detect_disagreement() can no longer produce: {set(DisagreementKind) - reachable}. "
        "If this is deliberate (e.g. a producer was retired), either restore a producer or "
        "remove the now-dead value from the SDK (major release, see its README "
        "'Compatibility' section) as was done for CANARY_APP_READINESS/HEARTBEAT_PROBE_RESULT."
    )
