"""Infra-011.12-011.16 Env x Stage contract tests."""

from __future__ import annotations

import pytest

from libs.pipeline_stage_contract import (
    BudgetStatus,
    DisagreementKind,
    FailureDomain,
    PipelineEnvironment,
    PipelineStage,
    StageStatus,
    acceleration_allowed,
    classify_budget,
    detect_disagreement,
    make_stage_result,
)


def test_env_stage_schema_serializes_required_fields() -> None:
    """Infra-011.12: producers share one sparse Env x Stage result shape."""
    result = make_stage_result(
        source="deploy-platform.yml",
        environment=PipelineEnvironment.STAGING,
        stage=PipelineStage.DEPLOY_STATUS,
        target="platform/postgres",
        status=StageStatus.PASS,
        duration_ms=42_000,
        evidence_url="https://github.com/wangzitian0/infra2/actions/runs/1",
    )

    payload = result.to_dict()

    assert payload == {
        "source": "deploy-platform.yml",
        "environment": "staging",
        "stage": "deploy-status",
        "target": "platform/postgres",
        "status": "pass",
        "duration_ms": 42_000,
        "deadline_ms": 1_200_000,
        "failure_domain": "none",
        "external_dependency": False,
        "suppressed_reason": "",
        "skipped_reason": "",
        "current_stage_age_ms": 0,
        "budget_status": "within-budget",
        "disagreement_kind": "none",
        "evidence_url": "https://github.com/wangzitian0/infra2/actions/runs/1",
    }


def test_failed_stage_requires_failure_domain() -> None:
    """Infra-011.13: failures cannot land as unclassified operator prose."""
    with pytest.raises(ValueError, match="failed stages must include failure_domain"):
        make_stage_result(
            source="cloudflare-watchdog",
            environment="staging",
            stage="config-preflight",
            target="WATCHDOG_TARGETS_JSON",
            status="fail",
            duration_ms=1,
        )


def test_preflight_external_dependency_is_explicit() -> None:
    """Infra-011.13: provider/config failures are modeled before expensive work."""
    result = make_stage_result(
        source="dokploy-route-canary.yml",
        environment="staging",
        stage="config-preflight",
        target="DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID",
        status="fail",
        duration_ms=300,
        failure_domain=FailureDomain.CONFIGURATION,
        external_dependency=True,
    )

    assert result.external_dependency is True
    assert result.failure_domain == FailureDomain.CONFIGURATION
    assert result.budget_status == BudgetStatus.WITHIN_BUDGET


def test_budget_classification_has_soft_and_hard_breaches() -> None:
    """Infra-011.14: speed tuning is driven by stage budget evidence."""
    assert (
        classify_budget(50_000, deadline_ms=120_000, soft_budget_ms=90_000)
        == BudgetStatus.WITHIN_BUDGET
    )
    assert (
        classify_budget(100_000, deadline_ms=120_000, soft_budget_ms=90_000)
        == BudgetStatus.SOFT_BREACH
    )
    assert (
        classify_budget(121_000, deadline_ms=120_000, soft_budget_ms=90_000)
        == BudgetStatus.HARD_BREACH
    )


def test_safe_acceleration_requires_skip_reason_and_never_allows_production() -> None:
    """Infra-011.16: speed skips need explicit evidence and stay out of prod."""
    staging_skip = make_stage_result(
        source="change-classifier",
        environment="staging",
        stage="integration",
        target="platform unchanged services",
        status="skip",
        skipped_reason="unchanged service set with deploy-smoke fallback",
    )
    production_skip = make_stage_result(
        source="change-classifier",
        environment="production",
        stage="integration",
        target="platform unchanged services",
        status="skip",
        skipped_reason="unchanged service set",
    )

    assert acceleration_allowed(staging_skip) is True
    assert acceleration_allowed(production_skip) is False

    with pytest.raises(ValueError, match="skipped stages must include"):
        make_stage_result(
            source="change-classifier",
            environment="staging",
            stage="integration",
            target="platform unchanged services",
            status="skip",
        )


def test_cross_stage_disagreement_is_measurable() -> None:
    """Infra-011.15: contradictory signals become deterministic records."""
    watchdog_pass = make_stage_result(
        source="infra-probes",
        environment="staging",
        stage="watchdog",
        target="platform-alerting-probes-staging",
        status="pass",
        duration_ms=500,
    )
    public_route_fail = make_stage_result(
        source="cloudflare-watchdog",
        environment="staging",
        stage="deploy-smoke",
        target="report-staging.zitian.party",
        status="fail",
        duration_ms=900,
        failure_domain=FailureDomain.TRAEFIK_PUBLIC_ROUTE,
    )

    assert (
        detect_disagreement([watchdog_pass, public_route_fail])
        == DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE
    )


def test_preview_stage_subset_is_named_and_stable() -> None:
    """Infra-011.12: pr-preview has a small runtime-focused stage surface."""
    preview_stages = {
        PipelineStage.REGRESSION_E2E,
        PipelineStage.IMAGE_BUILD,
        PipelineStage.DEPLOY_SMOKE,
        PipelineStage.ROUTE_CANARY,
    }

    assert {stage.value for stage in preview_stages} == {
        "regression-e2e",
        "image-build",
        "deploy-smoke",
        "route-canary",
    }
