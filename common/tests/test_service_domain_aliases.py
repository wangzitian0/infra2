"""service_domain and get_service_url keep the preview alias token (infra2-sdk#105)."""

from __future__ import annotations

import pytest

from common.core.environ import get_service_url, service_domain


def _env(name: str) -> dict[str, str]:
    return {"ENV": name, "INTERNAL_DOMAIN": "zitian.party"}


@pytest.mark.parametrize(
    ("env_name", "expected"),
    [
        ("production", "report.zitian.party"),
        ("staging", "report-staging.zitian.party"),
        ("pr-5", "report-pr-5.zitian.party"),
        ("pr_5", "report-pr-5.zitian.party"),
        ("branch_main", "report-branch-main.zitian.party"),
        ("canary_preview", "report-canary-preview.zitian.party"),
        ("preview", "report-preview.zitian.party"),
        ("dev_sandbox", "report-preview.zitian.party"),
    ],
)
def test_service_domain_keeps_the_preview_alias_token(
    env_name: str, expected: str
) -> None:
    assert service_domain("report", _env(env_name)) == expected


def test_get_service_url_keeps_the_preview_alias_token() -> None:
    url = get_service_url("portal", env=_env("pr_5"))
    assert url.startswith("https://")
    assert "-pr-5." in url
    assert "-preview." not in url
