"""Infra-009: finance_report fixed-env rate-limit capacity contract.

The hand-written secrets.ctmpl used to carry the staging/production fallback (#615);
since #637 the template is generated from the app manifest and the environment-specific
value lives where the deployment states configuration: the compose file's inline
default for production and AppDeployer.compose_env_overrides for staging.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "finance_report/finance_report/10.app/compose.yaml"
TEMPLATE = ROOT / "finance_report/finance_report/10.app/secrets.ctmpl"


def _deployer():
    spec = importlib.util.spec_from_file_location(
        "finance_report_app_deploy",
        ROOT / "finance_report/finance_report/10.app/deploy.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AppDeployer


def test_finance_report_rate_limit_fallback_is_environment_specific() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    default = re.search(
        r"API_RATE_LIMIT_REQUESTS: \$\{API_RATE_LIMIT_REQUESTS:-(?P<value>\d+)\}",
        compose,
    )
    assert default is not None and default.group("value") == "300"

    overrides = _deployer().compose_env_overrides
    assert (
        overrides(env="staging", domain="zitian.party", env_suffix="-staging")[
            "API_RATE_LIMIT_REQUESTS"
        ]
        == "2000"
    )
    assert "API_RATE_LIMIT_REQUESTS" not in overrides(
        env="production", domain="zitian.party", env_suffix=""
    )
    assert "API_RATE_LIMIT_REQUESTS" not in overrides(
        env="unexpected", domain="zitian.party", env_suffix="-x"
    )


def test_rate_limit_is_deployment_configuration_not_a_vault_value() -> None:
    """The generated template renders human / runtime values only; a rate limit is neither."""
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "API_RATE_LIMIT_REQUESTS" not in template


def test_finance_report_compose_env_base_merges_overrides() -> None:
    """#1279: AppDeployer.compose_env_base must forward to compose_env_overrides so
    the sync path and promote path produce consistent staging overrides."""
    deployer = _deployer()
    staging_base = deployer.compose_env_base(
        {
            "ENV": "staging",
            "INTERNAL_DOMAIN": "zitian.party",
            "ENV_DOMAIN_SUFFIX": "-staging",
            "ENV_SUFFIX": "-staging",
        }
    )
    assert staging_base["S3_BUCKET"] == "finance-report-staging"
    assert staging_base["S3_PUBLIC_BUCKET"] == "finance-report-staging"
    assert staging_base["API_RATE_LIMIT_REQUESTS"] == "2000"

    prod_base = deployer.compose_env_base(
        {
            "ENV": "production",
            "INTERNAL_DOMAIN": "zitian.party",
            "ENV_DOMAIN_SUFFIX": "",
        }
    )
    assert "S3_BUCKET" not in prod_base
    assert "API_RATE_LIMIT_REQUESTS" not in prod_base
