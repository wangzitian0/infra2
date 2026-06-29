"""The shared CI gate schema — the contract both repos' inventories validate against."""
from __future__ import annotations

from pathlib import Path

from libs.ci_gate_schema import load_delivery_stages, validate_gate, validate_inventory

ROOT = Path(__file__).resolve().parents[2]
STAGES = set(load_delivery_stages(ROOT / "docs/ssot/delivery-stages.yaml"))


def _gate(**over):
    base = dict(
        id="infra_ci.compose_validate",
        stage="github_ci.merge_authority",
        task_category="compose_validate",
        workflow=".github/workflows/infra-ci.yml",
        job="validate-compose",
    )
    base.update(over)
    return base


def test_valid_gate_passes() -> None:
    assert validate_gate(_gate(), stage_ids=STAGES) == []


def test_missing_required_field_rejected() -> None:
    gate = _gate()
    del gate["job"]
    assert any("job" in e for e in validate_gate(gate, stage_ids=STAGES))


def test_unknown_stage_rejected() -> None:
    errs = validate_gate(_gate(stage="nope.bad"), stage_ids=STAGES)
    assert any("unknown stage" in e for e in errs)


def test_repo_prefix_enforced() -> None:
    errs = validate_gate(_gate(id="ci.foo"), stage_ids=STAGES, id_prefix="infra_ci.")
    assert any("prefix" in e for e in errs)


def test_duplicate_ids_rejected() -> None:
    result = validate_inventory([_gate(), _gate()], stage_ids=STAGES)
    assert any("duplicate" in e for e in result["errors"])
