"""Tests for the per-env deploy config (the env axis, finance_report#883 P2)."""

from __future__ import annotations

import pytest

from tools import deploy_env_config as ec


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
