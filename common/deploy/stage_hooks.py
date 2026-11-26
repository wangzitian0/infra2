"""Control-plane stage hook planning and manifest loaders (absorbed from infra2-sdk v4.9.1)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from infra2_sdk.manifests import CELL_POLICIES, CellPolicy, RuntimeHooksManifest
from infra2_sdk.runtime.config_schema import EnvironmentManifest, _load_manifest


@dataclass(frozen=True)
class StageHookPlan:
    stage: str
    pre_checks: tuple[str, ...]
    operation_slot: str | None
    post_slot: str | None
    stage_id: str = field(kw_only=True)


def _derive_stage_plans(
    policies: Mapping[str, CellPolicy],
) -> Mapping[str, StageHookPlan]:
    plans: dict[str, StageHookPlan] = {}
    for st in ("preview", "staging", "prod", "schedule"):
        pre_p = policies.get(f"{st}.pre")
        op_p = policies.get(f"{st}.operation")
        post_p = (
            policies["scheduled_audit"] if st == "schedule" else policies[f"{st}.post"]
        )
        pre_checks = (
            pre_p.check_list if pre_p and isinstance(pre_p.check_list, tuple) else ()
        )
        op_slot = op_p.check_list if op_p and isinstance(op_p.check_list, str) else None
        post_slot = post_p.check_list if isinstance(post_p.check_list, str) else None
        plans[st] = StageHookPlan(
            st, pre_checks, op_slot, post_slot, stage_id=post_p.stage_id
        )
    return MappingProxyType(plans)


STAGE_HOOK_PLANS = _derive_stage_plans(CELL_POLICIES)


def stage_hook_plan(stage: str) -> StageHookPlan:
    if not isinstance(stage, str):
        raise ValueError(f"stage must be a string, got {type(stage).__name__}")
    key = stage.strip().lower()
    if key == "production":
        key = "prod"
    if key not in STAGE_HOOK_PLANS:
        raise ValueError(f"unknown stage {stage!r}; allowed: {list(STAGE_HOOK_PLANS)}")
    return STAGE_HOOK_PLANS[key]


def load(path: str | Path, *, root: Path | None = None) -> EnvironmentManifest:
    return _load_manifest(path, root=root)


def load_hooks_manifest(
    path: str | Path, *, root: Path | None = None
) -> RuntimeHooksManifest:
    target = Path(path) if root is None else root / path
    return RuntimeHooksManifest.from_dict(
        json.loads(target.read_text(encoding="utf-8"))
    )


__all__ = [
    "STAGE_HOOK_PLANS",
    "StageHookPlan",
    "load",
    "load_hooks_manifest",
    "stage_hook_plan",
]
