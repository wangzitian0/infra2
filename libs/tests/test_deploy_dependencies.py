"""Tests for the deploy dependency graph (fan-out + necessity audit).

Covers #267 / #261 P2: libs/ must NOT fan out to all services, own-dir and
declared deps must select exactly the right services, and the autoDeploy audit
must flag non-allowlisted Dokploy-native triggers.
"""

from libs.deploy_dependencies import (
    autodeploy_violations,
    load_dependency_manifest,
    match_changed_services,
    service_key_from_path,
)


def test_service_key_from_path_layouts():
    assert service_key_from_path("platform/24.openpanel/compose.yaml") == "platform/openpanel"
    assert (
        service_key_from_path("finance_report/finance_report/10.app/compose.yaml")
        == "finance_report/app"
    )
    # bootstrap dirs use dashes in the service key
    assert service_key_from_path("bootstrap/01.dokploy_install/x.sh") == "bootstrap/dokploy-install"
    # tooling / shared / root paths own no service
    assert service_key_from_path("libs/deployer.py") is None
    assert service_key_from_path("tools/x.py") is None
    assert service_key_from_path("common/foo.py") is None
    assert service_key_from_path("moon.yml") is None


def test_libs_change_fans_out_to_nothing():
    # The whole point: a shared-tooling change must NOT redeploy every service.
    assert match_changed_services(["libs/deployer.py"], manifest={}) == set()
    assert match_changed_services(["tools/dokploy_env.py"], manifest={}) == set()


def test_own_dir_change_selects_only_that_service():
    affected = match_changed_services(
        ["platform/24.openpanel/secrets.ctmpl"], manifest={}
    )
    assert affected == {"platform/openpanel"}


def test_multiple_own_dir_changes():
    affected = match_changed_services(
        [
            "platform/24.openpanel/compose.yaml",
            "platform/23.prefect/deploy.py",
            "finance_report/finance_report/10.app/compose.yaml",
        ],
        manifest={},
    )
    assert affected == {"platform/openpanel", "platform/prefect", "finance_report/app"}


def test_declared_dependency_fans_out_to_declarer_only():
    manifest = {"platform/openpanel": ["common/contracts/analytics.py"]}
    # a change to the declared shared file selects openpanel...
    assert match_changed_services(
        ["common/contracts/analytics.py"], manifest=manifest
    ) == {"platform/openpanel"}
    # ...but an unrelated common/ file selects nobody
    assert match_changed_services(["common/other.py"], manifest=manifest) == set()


def test_declared_dependency_glob():
    manifest = {"platform/signoz": ["common/clickhouse/*.xml"]}
    assert match_changed_services(
        ["common/clickhouse/users.xml"], manifest=manifest
    ) == {"platform/signoz"}
    assert match_changed_services(["common/clickhouse/readme.md"], manifest=manifest) == set()


def test_shipped_manifest_fans_libs_and_tools_to_alerting():
    # platform/alerting's Dockerfile bakes libs/ and tools/ into its image, so a
    # change to either MUST redeploy it. This guards against the manifest being
    # emptied back to a no-op (which would let alerting run stale tooling).
    manifest = load_dependency_manifest()
    assert "platform/alerting" in match_changed_services(["libs/deployer.py"], manifest=manifest)
    assert "platform/alerting" in match_changed_services(
        ["tools/dokploy_env.py"], manifest=manifest
    )
    # ...but a libs/ change still does NOT fan out to a service that does not
    # bake it in.
    assert "platform/openpanel" not in match_changed_services(
        ["libs/deployer.py"], manifest=manifest
    )


def test_autodeploy_violations():
    composes = [
        {"name": "openpanel", "autoDeploy": True},   # iac-managed -> violation
        {"name": "postgres", "autoDeploy": False},   # ok
        {"name": "vault", "autoDeploy": True},        # allowlisted -> ok
    ]
    allow = {"vault"}
    assert autodeploy_violations(composes, allow) == ["openpanel"]
    # with no allowlist, both true ones are violations
    assert autodeploy_violations(composes, set()) == ["openpanel", "vault"]
    # all off -> clean
    assert autodeploy_violations([{"name": "x", "autoDeploy": False}], allow) == []
