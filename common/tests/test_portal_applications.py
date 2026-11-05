"""The Homer portal lists the business applications and their dashboards (#1467).

Every test renders `platform/21.portal/config.yml.tmpl` through the real
`PortalDeployer._render_config`, for the staging and the production portal.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "zitian.party"
ENVIRONMENTS = {
    "production": {"INTERNAL_DOMAIN": DOMAIN, "ENV_DOMAIN_SUFFIX": ""},
    "staging": {"INTERNAL_DOMAIN": DOMAIN, "ENV_DOMAIN_SUFFIX": "-staging"},
}
DASHBOARD = ROOT / "finance_report/finance_report/observability/dashboard.json"

# Links that existed before #1467: name -> URL with `{S}` for ENV_DOMAIN_SUFFIX.
EXISTING_LINKS = {
    "Infra2 Repo": "https://github.com/wangzitian0/infra2",
    "Docs": "https://wangzitian0.github.io/infra2/",
    "Logout": "https://sso{S}.zitian.party/application/o/portal/end-session/",
    "Dokploy": "https://cloud.zitian.party",
    "Vault": "https://vault.zitian.party",
    "Authentik SSO": "https://sso{S}.zitian.party",
    "1Password Connect": "https://op.zitian.party/health",
    "Canary Todo": "https://todo{S}.zitian.party",
    "SigNoz": "https://signoz{S}.zitian.party",
    "SigNoz Dashboards": "https://signoz{S}.zitian.party/dashboard",
    "OpenPanel": "https://openpanel{S}.zitian.party",
    "Prefect": "https://prefect{S}.zitian.party",
    "S3 Console": "https://s3-console{S}.zitian.party",
    "Edge Watchdog": "https://infra2-cloudflare-watchdog.wangzitian-ai.workers.dev/status",
}
EXISTING_GROUPS = [
    "Platform Services",
    "Observability & Dashboards",
    "Out-of-Band & Health",
]


def _portal_module():
    spec = importlib.util.spec_from_file_location(
        "portal_applications_test", ROOT / "platform/21.portal/deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_text(environment: str) -> str:
    text = _portal_module().PortalDeployer._render_config(ENVIRONMENTS[environment])
    assert text, f"{environment} portal config did not render"
    return text


def _config(environment: str) -> dict:
    return yaml.safe_load(_render_text(environment))


def _groups(config: dict) -> dict[str, list[dict]]:
    return {g["name"]: g["items"] for g in config["services"]}


def _all_links(config: dict) -> dict[str, str]:
    links = {link["name"]: link["url"] for link in config["links"]}
    for items in _groups(config).values():
        for item in items:
            assert item["name"] not in links, f"duplicate link name {item['name']}"
            links[item["name"]] = item["url"]
    return links


def _signal_url(signal: str) -> str:
    """The production URL that docs/ssot/watchdog-signals.yaml holds for a signal."""
    signals = yaml.safe_load(
        (ROOT / "docs/ssot/watchdog-signals.yaml").read_text(encoding="utf-8")
    )["signals"]
    (url,) = [
        s["url"]
        for s in signals
        if s.get("signal") == signal and s.get("environment") == "production"
    ]
    return url


def _probe_target(environment: str, name: str) -> str:
    """The URL the in-band public-route probe checks for one environment."""
    from common.observability.probe_specs import render_public_route_spec_text

    for line in render_public_route_spec_text(environment, DOMAIN).splitlines():
        fields = line.split("|")
        if fields[0] == name:
            return fields[2]
    raise AssertionError(f"no public-route probe named {name}")


def _application_urls(environment: str) -> dict[str, str]:
    return {
        item["name"]: item["url"]
        for item in _groups(_config(environment))["Applications"]
    }


@pytest.mark.parametrize("environment", sorted(ENVIRONMENTS))
def test_applications_group_lists_both_apps_from_the_registry(environment: str) -> None:
    config = _config(environment)
    assert "Applications" in _groups(config)
    urls = _application_urls(environment)
    assert urls["Finance Report"] == _probe_target(
        environment, "finance-report-web-public-route"
    )
    assert urls["Truealpha"] == _probe_target(environment, "truealpha-web-public-route")


def test_production_application_links_equal_the_watchdog_urls() -> None:
    """Oracle outside the Deployer facets: the hand-kept watchdog signal table."""
    urls = _application_urls("production")
    assert urls["Finance Report"] == _signal_url("finance-report-web-public-route")
    assert urls["Truealpha"] == _signal_url("truealpha-web-public-route")


def test_staging_application_links_use_the_staging_hosts() -> None:
    urls = _application_urls("staging")
    assert urls["Finance Report"] == "https://report-staging.zitian.party/"
    assert urls["Truealpha"] == "https://truealpha-staging.truealpha.club/"


def test_no_link_points_to_another_environment() -> None:
    production = _all_links(_config("production"))
    assert [n for n, u in production.items() if "-staging" in u] == []

    staging = _config("staging")
    production_apps = _application_urls("production")
    for name, url in _application_urls("staging").items():
        if name.endswith("Dashboard"):
            continue
        assert url != production_apps[name], f"{name}: staging shows the production URL"
        assert "-staging" in urlparse(url).hostname, f"{name}: {url}"
    assert "Applications" in _groups(staging)


@pytest.mark.parametrize("environment", sorted(ENVIRONMENTS))
def test_dashboard_link_targets_the_one_signoz_instance(environment: str) -> None:
    """SigNoz is prod_only: no `signoz-staging` host exists, so both portals use `signoz`."""
    title = json.loads(DASHBOARD.read_text(encoding="utf-8"))["title"]
    url = _application_urls(environment)["Finance Report Dashboard"]
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https",
        f"signoz.{DOMAIN}",
        "/dashboard",
    )
    assert parse_qs(parsed.query) == {"search": [title]}
    subtitles = {
        i["name"]: i["subtitle"] for i in _groups(_config(environment))["Applications"]
    }
    assert subtitles["Finance Report Dashboard"] == title


def test_no_dashboard_link_for_an_app_without_a_dashboard() -> None:
    """truealpha ships no dashboard.json yet, so the portal must not invent a link."""
    assert not (ROOT / "truealpha/truealpha/observability/dashboard.json").exists()
    names = set(_application_urls("production"))
    assert "Truealpha Dashboard" not in names
    assert "Finance Report Dashboard" in names


def test_dashboard_links_follow_the_dashboard_files(tmp_path: Path) -> None:
    from common.core.registry import _LAYERS
    from common.observability.portal_apps import application_items

    def names(layers) -> set[str]:
        items = application_items(
            internal_domain=DOMAIN, domain_suffix="", layer_paths=layers
        )
        return {item["name"] for item in items}

    assert "Truealpha Dashboard" not in names(_LAYERS)

    layout = tmp_path / "observability"
    layout.mkdir()
    (layout / "dashboard.json").write_text(
        json.dumps({"title": "Truealpha — Web"}), encoding="utf-8"
    )
    added = names({**_LAYERS, "truealpha": tmp_path})
    assert "Truealpha Dashboard" in added
    assert "Finance Report Dashboard" in added

    without = names({**_LAYERS, "finance_report": tmp_path / "missing"})
    assert "Finance Report Dashboard" not in without
    assert "Finance Report" in without


def test_prod_only_application_has_no_staging_link() -> None:
    """A prod_only service has no staging host, so only the production portal links it."""
    import dataclasses

    from common.core.registry import service_attrs
    from common.observability.portal_apps import application_items

    attrs = dict(service_attrs())
    attrs["truealpha/app"] = dataclasses.replace(attrs["truealpha/app"], prod_only=True)

    def names(suffix: str) -> set[str]:
        items = application_items(
            internal_domain=DOMAIN, domain_suffix=suffix, attrs=attrs
        )
        return {item["name"] for item in items}

    assert "Truealpha" in names("")
    assert "Truealpha" not in names("-staging")
    assert "Finance Report" in names("-staging")


def test_application_items_fail_closed_without_a_registered_app() -> None:
    from common.observability.portal_apps import application_items

    with pytest.raises(ValueError, match="not registered"):
        application_items(internal_domain=DOMAIN, domain_suffix="", attrs={})


def test_render_returns_none_when_the_applications_cannot_be_derived(
    monkeypatch,
) -> None:
    module = _portal_module()

    def broken(**_kwargs):
        raise ValueError("no application is registered")

    monkeypatch.setattr(module, "application_items", broken)
    assert module.PortalDeployer._render_config(ENVIRONMENTS["production"]) is None


def test_render_returns_none_when_the_template_lost_its_placeholder(
    tmp_path: Path,
) -> None:
    """A template without the placeholder must not render an Applications group with no items."""
    import shutil

    copy = tmp_path / "21.portal"
    shutil.copytree(ROOT / "platform/21.portal", copy)
    template = copy / "config.yml.tmpl"
    text = template.read_text(encoding="utf-8")
    assert "{{APPLICATION_ITEMS}}" in text
    template.write_text(text.replace("{{APPLICATION_ITEMS}}", ""), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "portal_without_placeholder_test", copy / "deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PortalDeployer._render_config(ENVIRONMENTS["production"]) is None


def test_raw_template_stays_valid_yaml() -> None:
    """Tools that parse the unrendered template (test_todo_service) keep working."""
    raw = yaml.safe_load(
        (ROOT / "platform/21.portal/config.yml.tmpl").read_text(encoding="utf-8")
    )
    assert [g["name"] for g in raw["services"]][0] == "Applications"


@pytest.mark.parametrize("environment", sorted(ENVIRONMENTS))
def test_existing_links_are_unchanged(environment: str) -> None:
    suffix = ENVIRONMENTS[environment]["ENV_DOMAIN_SUFFIX"]
    links = _all_links(_config(environment))
    for name, template in EXISTING_LINKS.items():
        assert links.get(name) == template.format(S=suffix), name
    assert [g for g in _groups(_config(environment)) if g in EXISTING_GROUPS] == (
        EXISTING_GROUPS
    )


@pytest.mark.parametrize("environment", sorted(ENVIRONMENTS))
def test_generated_header_and_placeholders_are_kept(environment: str) -> None:
    text = _render_text(environment)
    assert text.startswith("---\n# DO NOT EDIT ON HOST - Generated from")
    assert "{{" not in text
    for item in _groups(yaml.safe_load(text))["Applications"]:
        assert set(item) >= {"name", "icon", "subtitle", "tag", "url", "target"}


def test_portal_hash_reads_every_file_that_shapes_the_applications() -> None:
    """A registry fact edit must flip the portal hash, or sync skips the new file."""
    from common.deploy.config_hash import _dependency_paths_from_globs
    from common.deploy.dependencies import extra_dependency_globs

    hashed = {
        p.relative_to(ROOT).as_posix()
        for p in _dependency_paths_from_globs(extra_dependency_globs("platform/portal"))
    }
    assert {
        "common/observability/portal_apps.py",
        "common/observability/probe_specs.py",
        "finance_report/finance_report/10.app/deploy.py",
        "truealpha/truealpha/10.app/deploy.py",
        "platform/11.signoz/deploy.py",
        "finance_report/finance_report/observability/dashboard.json",
    } <= hashed
