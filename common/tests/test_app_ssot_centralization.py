"""Structural SSOT tests for App registries, upstream manifests, and observability discovery (#1504)."""

from __future__ import annotations

import ast
from pathlib import Path

from common.core import constants, registry
from common.deploy import app_contract
from common import observability_dashboards as obs

ROOT = Path(__file__).resolve().parents[2]


def test_app_sources_derives_dynamically_from_repos_yaml(tmp_path: Path) -> None:
    """When harness/repos.yaml adds a 4th app `alpha_lab`, _load_app_sources_from_repos_yaml
    includes it automatically without editing common/core/constants.py."""
    assert constants.APP_SOURCES == {
        "finance_report/app": "wangzitian0/finance_report",
        "truealpha/app": "wangzitian0/truealpha",
    }

    synthetic_repos_yaml = tmp_path / "repos.yaml"
    synthetic_repos_yaml.write_text(
        (ROOT / "harness" / "repos.yaml").read_text(encoding="utf-8")
        + "\n  - id: alpha-lab\n"
        + "    path: repos/alpha_lab\n"
        + "    role: external-application\n"
        + "    source: https://github.com/wangzitian0/alpha_lab\n"
        + "    release_tag: v0.1.0\n",
        encoding="utf-8",
    )
    derived = constants._load_app_sources_from_repos_yaml(synthetic_repos_yaml)
    assert derived == {
        "finance_report/app": "wangzitian0/finance_report",
        "truealpha/app": "wangzitian0/truealpha",
        "alpha_lab/app": "wangzitian0/alpha_lab",
    }

    # Also supports the compact `repos:` mapping shape
    compact_yaml = tmp_path / "compact_repos.yaml"
    compact_yaml.write_text(
        "repos:\n"
        "  alpha_lab:\n"
        "    repo: wangzitian0/alpha_lab\n"
        "    release_tag: v0.1.0\n",
        encoding="utf-8",
    )
    assert constants._load_app_sources_from_repos_yaml(compact_yaml) == {
        "alpha_lab/app": "wangzitian0/alpha_lab",
    }


def test_app_upstream_manifests_derives_from_service_registry() -> None:
    """APP_UPSTREAM_MANIFESTS is derived from ServiceMeta.upstream_manifests across all
    registered App services in common/core/registry.py and harness/repos.yaml."""
    attrs = registry.service_attrs()
    derived = app_contract._derive_app_upstream_manifests(attrs)
    assert derived == app_contract.APP_UPSTREAM_MANIFESTS
    assert derived == {
        "finance_report": (
            ("finance_report/app", "common/runtime/required-env.generated.json"),
        ),
        "truealpha": (
            ("truealpha/app", "apps/app-web/required-env.manifest.json"),
            ("truealpha/app", "apps/llm-service/required-env.generated.json"),
            ("truealpha/data_engine", "apps/data-engine/required-env.generated.json"),
        ),
    }

    # Every App project in PROJECT_REPOS is represented in APP_UPSTREAM_MANIFESTS
    assert set(constants.PROJECT_REPOS) == set(app_contract.APP_UPSTREAM_MANIFESTS)

    # Adding a synthetic ServiceMeta with upstream_manifests automatically populates the map
    synthetic_meta = registry.ServiceMeta(
        service_id="alpha_lab/app",
        layer="alpha_lab",
        service="app",
        prod_only=False,
        subdomain=None,
        service_port=3000,
        service_name="web",
        telemetry_service_name="alpha-lab-app",
        telemetry_component="app",
        project="alpha_lab",
        upstream_manifests=("apps/alpha-lab/required-env.generated.json",),
    )
    extended = app_contract._derive_app_upstream_manifests(
        {**attrs, "alpha_lab/app": synthetic_meta}
    )
    assert extended["alpha_lab"] == (
        ("alpha_lab/app", "apps/alpha-lab/required-env.generated.json"),
    )


def test_observability_files_are_dynamically_discovered_from_service_dirs(
    tmp_path: Path,
) -> None:
    """DASHBOARD_FILES and ALERT_RULES_FILES are dynamically discovered from service
    directories without static tuple edits."""
    assert obs._discover_alert_rules_files(ROOT) == obs.ALERT_RULES_FILES
    assert obs._discover_dashboard_files(ROOT) == obs.DASHBOARD_FILES
    assert tuple(p.relative_to(ROOT).as_posix() for p in obs.ALERT_RULES_FILES) == (
        "finance_report/finance_report/observability/alert_rules.json",
        "truealpha/truealpha/observability/alert_rules.json",
        "platform/12.alerting/alert_rules.json",
    )
    assert tuple(p.relative_to(ROOT).as_posix() for p in obs.DASHBOARD_FILES) == (
        "finance_report/finance_report/observability/dashboard.json",
        "truealpha/truealpha/observability/dashboard.json",
        "platform/12.alerting/dashboard.json",
        "platform/30.todo/dashboard.json",
    )

    # Synthetic service directory with observability/{dashboard,alert_rules}.json is discovered
    new_service_obs = tmp_path / "platform" / "40.synthetic" / "observability"
    new_service_obs.mkdir(parents=True)
    new_dash = new_service_obs / "dashboard.json"
    new_rules = new_service_obs / "alert_rules.json"
    new_dash.write_text("{}", encoding="utf-8")
    new_rules.write_text("{}", encoding="utf-8")

    app_service_obs = tmp_path / "apps" / "24.alpha_lab" / "observability"
    app_service_obs.mkdir(parents=True)
    app_dash = app_service_obs / "dashboard.json"
    app_rules = app_service_obs / "alert_rules.json"
    app_dash.write_text("{}", encoding="utf-8")
    app_rules.write_text("{}", encoding="utf-8")

    assert obs._discover_dashboard_files(tmp_path) == (new_dash, app_dash)
    assert obs._discover_alert_rules_files(tmp_path) == (new_rules, app_rules)


def test_ast_guard_no_hardcoded_observability_file_tuples() -> None:
    """AST guard: ALERT_RULES_FILES, DASHBOARD_FILES, APP_SOURCES, and APP_UPSTREAM_MANIFESTS
    must be derived via helper calls rather than static tuple/dict literals."""
    obs_tree = ast.parse(
        (ROOT / "common" / "observability_dashboards.py").read_text(encoding="utf-8")
    )
    for node in obs_tree.body:
        target_names: list[str] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if "ALERT_RULES_FILES" in target_names or "DASHBOARD_FILES" in target_names:
            assert isinstance(value, ast.Call), (
                f"{target_names} must be derived via a discovery call, got {ast.dump(value)}"
            )

    const_tree = ast.parse(
        (ROOT / "common" / "core" / "constants.py").read_text(encoding="utf-8")
    )
    for node in const_tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "APP_SOURCES"
        ):
            assert isinstance(node.value, ast.Call), (
                "APP_SOURCES must be derived via _load_app_sources_from_repos_yaml()"
            )
