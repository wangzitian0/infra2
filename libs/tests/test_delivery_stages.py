"""The authoritative delivery-stage vocabulary (single-owner contract)."""
from __future__ import annotations

import re
from pathlib import Path

from infra2_sdk.ci import load_delivery_stages

ROOT = Path(__file__).resolve().parents[2]
STAGES = load_delivery_stages(ROOT / "docs/ssot/delivery-stages.yaml")
# multi-segment ids allowed (e.g. github_ci.merge_authority, and future a.b.c) — aligns
# with infra2_sdk.ci.GATE_ID_RE so the stage vocab and gate ids share one grammar.
_ID = re.compile(r"\A[a-z][a-z_]*(?:\.[a-z_]+)+\Z")


def test_stage_ids_well_formed_and_unique() -> None:
    assert STAGES, "stage vocabulary must not be empty"
    assert all(_ID.match(sid) for sid in STAGES), sorted(STAGES)


def test_stage_orders_total_and_unique() -> None:
    orders = [s["order"] for s in STAGES.values()]
    assert len(orders) == len(set(orders)), "stage orders must be unique"
    assert sorted(orders) == list(range(len(orders))), "stage orders must be a 0..n-1 total order"


def test_every_stage_has_a_definition() -> None:
    assert all(str(s.get("definition", "")).strip() for s in STAGES.values())
