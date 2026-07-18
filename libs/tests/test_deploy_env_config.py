"""Tests for the per-env deploy config (the env axis, finance_report#883 P2)."""

from __future__ import annotations

import pytest

from libs import deploy_env_config as ec


def test_staging_config():
    cfg = ec.env_config("staging")
    assert cfg.compose_id == "A6V-hbJlgHMwgPDoTDnhH"
    assert cfg.env_suffix == "-staging"
    assert cfg.data_default == "staging"
    assert cfg.gates_prod is True
    assert cfg.requires_staging_first is False
    assert cfg.app_url(domain="zitian.party") == "https://report-staging.zitian.party"


def test_prod_config():
    cfg = ec.env_config("prod")
    assert cfg.compose_id == "lNn9gVS1Zyw79Jzw5dlbu"
    assert cfg.env_suffix == ""
    assert cfg.data_default == "prod"  # prod is always real prod data
    assert cfg.requires_staging_first is True
    assert cfg.app_url(domain="zitian.party") == "https://report.zitian.party"


def test_preview_is_dynamic_and_defaults_to_staging_data():
    cfg = ec.env_config("preview")
    assert cfg.dynamic is True
    assert cfg.compose_id is None  # per-PR, resolved by the lifecycle
    assert cfg.data_default == "staging"  # non-prod default (operator choice)
    assert (
        cfg.app_url(domain="zitian.party", number=42)
        == "https://report-pr-42.zitian.party"
    )


def test_unknown_env_raises():
    with pytest.raises(ValueError, match="unknown deploy env"):
        ec.env_config("qa")


def test_env_suffix_helper_handles_dynamic_preview():
    assert ec.for_env_suffix("staging") == "-staging"
    assert ec.for_env_suffix("prod") == ""
    assert ec.for_env_suffix("preview", number=7) == "-pr-7"


def test_with_compose_id_binds_the_dynamic_preview_compose():
    bound = ec.with_compose_id("preview", "cmp-123")
    assert bound.compose_id == "cmp-123"
    assert bound.name == "preview"
    # the original mapping is unchanged (frozen dataclass, replace returns a copy)
    assert ec.env_config("preview").compose_id is None


def test_every_non_dynamic_env_has_a_compose_id():
    for name in ec.ENVIRONMENTS:
        cfg = ec.env_config(name)
        if not cfg.dynamic:
            assert cfg.compose_id, f"{name} must declare a compose_id"


# --- bespoke_app_compose_targets (#524 live drift verification) --------------------


def test_bespoke_app_compose_targets_includes_finance_report_staging_and_prod():
    targets = {(t.service, t.env): t for t in ec.bespoke_app_compose_targets()}
    staging = targets[("finance_report/app", "staging")]
    assert staging.project_name == "finance_report"
    assert staging.compose_name == "app"
    assert staging.dokploy_env_name == "staging"
    assert staging.compose_id == "A6V-hbJlgHMwgPDoTDnhH"

    prod = targets[("finance_report/app", "prod")]
    assert prod.project_name == "finance_report"
    assert prod.compose_name == "app"
    assert prod.dokploy_env_name == "production"  # normalize_env_name("prod")
    assert prod.compose_id == "lNn9gVS1Zyw79Jzw5dlbu"


def test_bespoke_app_compose_targets_includes_registered_overrides():
    targets = {(t.service, t.env): t for t in ec.bespoke_app_compose_targets()}
    truealpha_staging = targets[("truealpha/app", "staging")]
    assert truealpha_staging.project_name == "truealpha"
    assert truealpha_staging.compose_name == "app"
    assert truealpha_staging.dokploy_env_name == "staging"
    assert truealpha_staging.compose_id == "w4zo_fm9d2PnUY8ULzNO7"


def test_bespoke_app_compose_targets_skips_unregistered_and_dynamic_entries():
    targets = {(t.service, t.env) for t in ec.bespoke_app_compose_targets()}
    # truealpha/app prod has compose_id=None (not registered yet) — never a target.
    assert ("truealpha/app", "prod") not in targets
    # preview is dynamic/per-PR — never a fixed target.
    assert ("finance_report/app", "preview") not in targets


def test_bespoke_app_compose_targets_count_matches_registered_non_none_entries():
    # 2 finance_report envs (staging, prod) + 1 truealpha override (staging only,
    # prod is None) = 3. A drift check silently skipping an entry would under-count.
    assert len(ec.bespoke_app_compose_targets()) == 3


# --- preview alias model (multi-alias preview env) -------------------------------


def test_preview_alias_branch_main():
    a = ec.preview_alias("branch", "main")
    assert a.kind == "branch"
    assert a.value == "main"
    assert a.alias == "branch-main"  # uniform <kind>-<value>, no bare special case
    assert a.env_suffix == "-branch-main"
    assert a.domain_suffix == "-branch-main"
    assert a.compose_name == "finance-report-preview-branch-main"
    assert a.deployment_environment == "branch-main"
    assert a.app_url(domain="zitian.party") == "https://report-branch-main.zitian.party"


def test_preview_alias_branch_defaults_to_main():
    assert ec.preview_alias("branch").alias == "branch-main"


def test_preview_alias_pr():
    a = ec.preview_alias("pr", 5)
    assert a.alias == "pr-5"
    assert a.env_suffix == "-pr-5"
    assert a.compose_name == "finance-report-preview-pr-5"
    assert a.deployment_environment == "pr-5"
    assert a.app_url(domain="zitian.party") == "https://report-pr-5.zitian.party"


def test_preview_alias_pr_accepts_string_value():
    assert ec.preview_alias("pr", "42").alias == "pr-42"


def test_preview_alias_commit_truncates_to_short_sha():
    long_sha = "1ab32d5e6f7089abcdef0123456789abcdef0123"
    a = ec.preview_alias("commit", long_sha)
    assert a.value == "1ab32d5"  # short sha matches IMAGE_TAG / service.version
    assert a.alias == "commit-1ab32d5"
    assert a.env_suffix == "-commit-1ab32d5"
    assert a.compose_name == "finance-report-preview-commit-1ab32d5"
    assert a.deployment_environment == "commit-1ab32d5"
    assert (
        a.app_url(domain="zitian.party") == "https://report-commit-1ab32d5.zitian.party"
    )


def test_preview_alias_commit_lowercases():
    assert ec.preview_alias("commit", "ABCDEF1").value == "abcdef1"


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "", "1.5"])
def test_preview_alias_pr_rejects_non_positive_int(bad):
    with pytest.raises(ValueError, match="positive PR number"):
        ec.preview_alias("pr", bad)


@pytest.mark.parametrize("bad", ["xyz", "12345", "g123456", ""])
def test_preview_alias_commit_rejects_non_sha(bad):
    with pytest.raises(ValueError, match="hex commit sha"):
        ec.preview_alias("commit", bad)


def test_preview_alias_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown preview kind"):
        ec.preview_alias("release", "v1.0.0")


def test_preview_aliases_are_unique_per_kind_value():
    # the coexisting kinds must never collide on suffix / compose name
    names = {
        ec.preview_alias("branch", "main").compose_name,
        ec.preview_alias("pr", 5).compose_name,
        ec.preview_alias("commit", "1ab32d5").compose_name,
        ec.preview_alias("tag", "v1.2.3").compose_name,
    }
    assert len(names) == 4


def test_preview_alias_is_frozen():
    a = ec.preview_alias("pr", 5)
    with pytest.raises(Exception):
        a.alias = "pr-6"  # type: ignore[misc]


def test_preview_tag_alias_is_dns_safe():
    from libs.deploy_env_config import preview_alias

    a = preview_alias("tag", "v1.2.3")
    assert a.value == "v1.2.3"  # canonical tag kept for the image ref
    assert a.alias == "tag-v1-2-3"  # dots -> dashes for the single DNS label
    assert a.env_suffix == "-tag-v1-2-3"
    assert a.app_url(domain="zitian.party") == "https://report-tag-v1-2-3.zitian.party"


def test_preview_tag_alias_rejects_non_tag():
    import pytest

    from libs.deploy_env_config import preview_alias

    with pytest.raises(ValueError, match="vX.Y.Z release tag"):
        preview_alias("tag", "1.2.3")  # missing leading v
    with pytest.raises(ValueError, match="vX.Y.Z release tag"):
        preview_alias("tag", "main")
