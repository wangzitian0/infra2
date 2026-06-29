#!/usr/bin/env python3
"""Shared schema for CI gate inventory entries — the contract BOTH repos conform to.

A CI gate is one coordinate: ``(stage, task_category) -> workflow:job`` plus metadata —
the CI analogue of ``deploy_v2``'s ``(service, type, version_ref, iac_ref)``. This module
is the single owner of (a) the gate field contract and (b) the loader for the authoritative
stage vocabulary in ``docs/ssot/delivery-stages.yaml``. finance_report and infra2 each keep
their OWN inventory of gate instances and validate against this; neither redefines stages
locally, and gate ids are namespaced by repo prefix so the two inventories cannot overlap.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

GATE_ID_RE = re.compile(r"\A[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
REQUIRED_GATE_FIELDS = ("id", "stage", "task_category", "workflow", "job")


def load_delivery_stages(path: str | Path) -> dict[str, dict]:
    """Return the authoritative stage vocabulary as ``{stage_id: {order, definition}}``."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    stages = data.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("delivery-stages.yaml: 'stages' must be a mapping id -> {order, definition}")
    return stages


def validate_gate(raw: dict, *, stage_ids: set[str], id_prefix: str | None = None) -> list[str]:
    """Return human-readable errors for one gate dict; empty list == valid."""
    errors: list[str] = []
    for field in REQUIRED_GATE_FIELDS:
        if not str(raw.get(field, "")).strip():
            errors.append(f"missing required field {field!r}")
    gid = str(raw.get("id", ""))
    if gid and not GATE_ID_RE.match(gid):
        errors.append(f"gate id {gid!r} must match {GATE_ID_RE.pattern}")
    if id_prefix and gid and not gid.startswith(id_prefix):
        errors.append(f"gate id {gid!r} must carry the repo prefix {id_prefix!r}")
    stage = raw.get("stage")
    if stage and stage not in stage_ids:
        errors.append(f"unknown stage {stage!r} (not declared in delivery-stages.yaml)")
    return errors


def validate_inventory(gates: list[dict], *, stage_ids: set[str], id_prefix: str | None = None) -> dict:
    """Validate a whole inventory: per-gate schema + unique ids. Returns {errors, ids}."""
    errors: list[str] = []
    seen: set[str] = set()
    for i, gate in enumerate(gates):
        for err in validate_gate(gate, stage_ids=stage_ids, id_prefix=id_prefix):
            errors.append(f"gate[{i}] ({gate.get('id', '?')}): {err}")
        gid = str(gate.get("id", ""))
        if gid and gid in seen:
            errors.append(f"duplicate gate id {gid!r}")
        if gid:
            seen.add(gid)
    return {"errors": errors, "ids": sorted(seen)}
