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

from libs.common import SERVICE_SUBDOMAINS

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
    "MINIO_CONSOLE_URL",
    "MINIO_API_URL",
    "FINANCE_REPORT_BASE",
    "FINANCE_REPORT_URL",
    "FINANCE_REPORT_API_URL",
    "PORTAL_URL",
)

# (DEPLOY_ENV, PR_NUMBER, host suffix of the environment)
ENVIRONMENTS = [
    pytest.param("production", None, "", id="production"),
    pytest.param("staging", None, "-staging", id="staging"),
    pytest.param("pr-test", "47", "-pr-47", id="pr-test"),
]

# URL name -> SERVICE_SUBDOMAINS key that the default URL must use.
# The keys are the current names. The legacy keys `minio_console` and `minio_api`
# name the old host `minio`, and they must not decide a default host.
VALIDATED_URLS = {
    "DOKPLOY_URL": "dokploy",
    "OP_URL": "1password",
    "VAULT_URL": "vault",
    "SSO_URL": "sso",
    "MINIO_CONSOLE_URL": "s3_console",
    "MINIO_API_URL": "s3",
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
    config = load_test_config("production", MINIO_CONSOLE_URL=f"https://minio.{DOMAIN}")

    with pytest.raises(RuntimeError) as raised:
        config.validate()

    assert str(raised.value) == (
        f"MINIO_CONSOLE_URL host mismatch. Expected s3-console.{DOMAIN}, "
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
        MINIO_CONSOLE_URL=f"https://{FOREIGN_HOST}",
        E2E_ALLOW_CUSTOM_DOMAIN="true",
    )

    assert config.validate() is None
    assert config.MINIO_CONSOLE_URL == f"https://{FOREIGN_HOST}"
