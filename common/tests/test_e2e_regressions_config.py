"""Offline test for the host check in e2e_regressions/conftest.py (#1045).

The daily production smoke job failed because `TestConfig.validate()` expected
the host `minio.zitian.party` while the default console URL used
`s3-console.zitian.party`. Pull request CI never ran `validate()`, so only the
production job showed the fault.

This test loads the real conftest file with a controlled environment.
It makes no network call and it does not read 1Password.
The `playwright` import is a stub, because infra-ci does not install the `e2e`
dependency group.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

from common.core.environ import SERVICE_SUBDOMAINS

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = ROOT / "e2e_regressions" / "conftest.py"
DOMAIN = "zitian.party"
FOREIGN_HOST = "foreign.example.com"

# Every environment variable that the conftest reads for a URL or a domain.
# The test clears all of them, so a value on the developer machine cannot change the result.
CLEARED_ENV = (
    "DEPLOY_ENV",
    "PR_NUMBER",
    "INTERNAL_DOMAIN",
    "BASE_DOMAIN",
    "E2E_ALLOW_CUSTOM_DOMAIN",
    "DOKPLOY_URL",
    "OP_URL",
    "VAULT_URL",
    "SSO_URL",
    "S3_CONSOLE_URL",
    "S3_API_URL",
    "FINANCE_REPORT_BASE",
    "FINANCE_REPORT_URL",
    "FINANCE_REPORT_API_URL",
    "PORTAL_URL",
    "PLATFORM_DB_HOST",
    "PLATFORM_DB_PORT",
    "PLATFORM_DB_USER",
    "PLATFORM_DB_PASSWORD",
    "PLATFORM_DB_NAME",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "PG_HOST",
    "PG_PORT",
    "PG_USER",
    "PG_PASS",
    "PG_DB",
)

# (DEPLOY_ENV, PR_NUMBER, host suffix of the environment)
ENVIRONMENTS = [
    pytest.param("production", None, "", id="production"),
    pytest.param("staging", None, "-staging", id="staging"),
    pytest.param("pr-test", "47", "-pr-47", id="pr-test"),
]

# URL name -> SERVICE_SUBDOMAINS key that the default URL must use.
# The keys are canonical names; retired legacy keys are eliminated.
VALIDATED_URLS = {
    "DOKPLOY_URL": "dokploy",
    "OP_URL": "1password",
    "VAULT_URL": "vault",
    "SSO_URL": "sso",
    "S3_CONSOLE_URL": "s3_console",
    "S3_API_URL": "s3",
}


@pytest.fixture
def load_test_config(monkeypatch):
    """Return a loader that executes the real conftest and returns its TestConfig."""

    def _load(deploy_env, pr_number=None, **env):
        for name in CLEARED_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DEPLOY_ENV", deploy_env)
        monkeypatch.setenv("INTERNAL_DOMAIN", DOMAIN)
        # BASE_DOMAIN set: the conftest then skips its 1Password fallback.
        monkeypatch.setenv("BASE_DOMAIN", DOMAIN)
        if pr_number:
            monkeypatch.setenv("PR_NUMBER", pr_number)
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        # The conftest edits sys.path at import time. Restore it after the test.
        monkeypatch.setattr(sys, "path", list(sys.path))
        playwright = types.ModuleType("playwright")
        async_api = types.ModuleType("playwright.async_api")
        async_api.async_playwright = object
        async_api.Browser = object
        async_api.BrowserContext = object
        async_api.Page = object
        playwright.async_api = async_api
        monkeypatch.setitem(sys.modules, "playwright", playwright)
        monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)

        # TestConfig reads the environment when the module executes, so each call
        # executes the file again. The module stays out of sys.modules.
        spec = importlib.util.spec_from_file_location("_e2e_conftest", CONFTEST)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.TestConfig

    return _load


@pytest.mark.parametrize(("deploy_env", "pr_number", "suffix"), ENVIRONMENTS)
def test_default_config_passes_its_own_host_check(
    load_test_config, deploy_env, pr_number, suffix
):
    config = load_test_config(deploy_env, pr_number)

    assert config.validate() is None
    # The default URLs use the current subdomain keys, not the legacy keys.
    for url_name, key in VALIDATED_URLS.items():
        expected = f"https://{SERVICE_SUBDOMAINS[key]}{suffix}.{DOMAIN}"
        assert getattr(config, url_name) == expected, url_name


def test_legacy_console_host_is_rejected_with_the_expected_host(load_test_config):
    """The production fault in reverse: the old host `minio` is now a mismatch."""
    config = load_test_config("production", S3_CONSOLE_URL=f"https://minio.{DOMAIN}")

    with pytest.raises(RuntimeError) as raised:
        config.validate()

    assert str(raised.value) == (
        f"S3_CONSOLE_URL host mismatch. Expected s3-console.{DOMAIN}, "
        f"got minio.{DOMAIN}. "
        "Set E2E_ALLOW_CUSTOM_DOMAIN=true to override."
    )


@pytest.mark.parametrize("url_name", sorted([*VALIDATED_URLS, "FINANCE_REPORT_URL"]))
def test_every_checked_url_rejects_a_foreign_host(load_test_config, url_name):
    if url_name == "FINANCE_REPORT_URL":
        # The API URL must share the host of the app URL, so move both together.
        config = load_test_config("production", FINANCE_REPORT_BASE=FOREIGN_HOST)
        expected_host = f"report.{DOMAIN}"
    else:
        config = load_test_config("production", **{url_name: f"https://{FOREIGN_HOST}"})
        key = VALIDATED_URLS[url_name]
        expected_host = f"{SERVICE_SUBDOMAINS[key]}.{DOMAIN}"

    with pytest.raises(
        RuntimeError,
        match=re.escape(
            f"{url_name} host mismatch. Expected {expected_host}, got {FOREIGN_HOST}."
        ),
    ):
        config.validate()


def test_custom_domain_flag_skips_the_host_check(load_test_config):
    config = load_test_config(
        "production",
        S3_CONSOLE_URL=f"https://{FOREIGN_HOST}",
        E2E_ALLOW_CUSTOM_DOMAIN="true",
    )

    assert config.validate() is None
    assert config.S3_CONSOLE_URL == f"https://{FOREIGN_HOST}"


def test_platform_db_reads_canonical_keys_and_ignores_legacy_aliases(load_test_config):
    config = load_test_config(
        "production",
        DB_HOST="legacy-db.internal",
        DB_PORT="15432",
        DB_USER="legacy_user",
        DB_PASSWORD="legacy_password",
        PG_PASS="legacy_pg_pass",
    )
    assert config.PLATFORM_DB_HOST is None
    assert config.PLATFORM_DB_PORT == "5432"
    assert config.PLATFORM_DB_USER == "postgres"
    assert config.PLATFORM_DB_PASSWORD == ""
    assert config.PLATFORM_DB_NAME == "postgres"

    configured = load_test_config(
        "production",
        PLATFORM_DB_HOST="pg.internal",
        PLATFORM_DB_PORT="6432",
        PLATFORM_DB_USER="admin",
        PLATFORM_DB_PASSWORD="secret",
        PLATFORM_DB_NAME="authentik",
    )
    assert configured.PLATFORM_DB_HOST == "pg.internal"
    assert configured.PLATFORM_DB_PORT == "6432"
    assert configured.PLATFORM_DB_USER == "admin"
    assert configured.PLATFORM_DB_PASSWORD == "secret"
    assert configured.PLATFORM_DB_NAME == "authentik"


def test_e2e_env_example_keys_are_all_read_in_e2e_suite():
    env_example = ROOT / "e2e_regressions" / ".env.example"
    declared = set()
    for raw in env_example.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        declared.add(line.split("=", 1)[0].strip())

    assert declared
    py_sources = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "e2e_regressions").rglob("*.py"))
    )
    unread = sorted(
        key
        for key in declared
        if f'"{key}"' not in py_sources and f"'{key}'" not in py_sources
    )
    assert unread == []
