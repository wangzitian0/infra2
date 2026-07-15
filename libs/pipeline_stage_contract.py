"""Compatibility imports for the SDK-owned Env x Stage evidence contract.

New cross-repository consumers import :mod:`infra2_sdk.delivery` directly. The
``libs`` path remains during the migration so existing infra producers do not need an
all-at-once import rewrite.
"""

from infra2_sdk.delivery import (
    PREVIEW_RELEVANT_STAGES,
    STAGE_DEADLINE_MS,
    BudgetStatus,
    DisagreementKind,
    FailureDomain,
    PipelineEnvironment,
    PipelineStage,
    StageResult,
    StageStatus,
    acceleration_allowed,
    classify_budget,
    detect_disagreement,
    make_stage_result,
    validate_stage_result,
)

__all__ = [
    "PREVIEW_RELEVANT_STAGES",
    "STAGE_DEADLINE_MS",
    "BudgetStatus",
    "DisagreementKind",
    "FailureDomain",
    "PipelineEnvironment",
    "PipelineStage",
    "StageResult",
    "StageStatus",
    "acceleration_allowed",
    "classify_budget",
    "detect_disagreement",
    "make_stage_result",
    "validate_stage_result",
]
