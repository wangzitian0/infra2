"""Tests for the deploy dependency graph (fan-out + necessity audit).

Covers #267 / #261 P2: libs/ must NOT fan out to all services, own-dir and
declared deps must select exactly the right services, and the autoDeploy audit
must flag non-allowlisted Dokploy-native triggers.
"""

from libs.deploy_dependencies import (
    autodeploy_violations,
    dockerfile_baked_shared_trees,
    explain_fanout,
    fanout_coverage_violations,
    load_dependency_manifest,
    match_changed_services,
    service_key_from_path,
)


def test_service_key_from_path_layouts():
    assert (
        service_key_from_path("platform/24.openpanel/compose.yaml")
        == "platform/openpanel"
    )
    assert (
        service_key_from_path("finance_report/finance_report/10.app/compose.yaml")
        == "finance_report/app"
    )
    # bootstrap dirs use dashes in the service key
    assert (
        service_key_from_path("bootstrap/01.dokploy_install/x.sh")
        == "bootstrap/dokploy-install"
    )
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
    assert (
        match_changed_services(["common/clickhouse/readme.md"], manifest=manifest)
        == set()
    )


def test_shipped_manifest_fans_libs_and_tools_to_alerting():
    # platform/alerting's Dockerfile bakes libs/ and tools/ into its image, so a
    # change to either MUST redeploy it. This guards against the manifest being
    # emptied back to a no-op (which would let alerting run stale tooling).
    manifest = load_dependency_manifest()
    assert "platform/alerting" in match_changed_services(
        ["libs/deployer.py"], manifest=manifest
    )
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
        {"name": "openpanel", "autoDeploy": True},  # iac-managed -> violation
        {"name": "postgres", "autoDeploy": False},  # ok
        {"name": "vault", "autoDeploy": True},  # allowlisted -> ok
    ]
    allow = {"vault"}
    assert autodeploy_violations(composes, allow) == ["openpanel"]
    # with no allowlist, both true ones are violations
    assert autodeploy_violations(composes, set()) == ["openpanel", "vault"]
    # all off -> clean
    assert autodeploy_violations([{"name": "x", "autoDeploy": False}], allow) == []


# --- Observability -----------------------------------------------------------


def test_explain_fanout_records_reasons_and_drops():
    manifest = {"platform/alerting": ["libs/**", "tools/**"]}
    decision = explain_fanout(
        [
            "platform/24.openpanel/compose.yaml",  # own-dir
            "libs/deployer.py",  # declared dep of alerting; drop for openpanel
            "docs/notes.md",  # owned by nobody -> dropped
        ],
        manifest=manifest,
    )
    assert decision.selected["platform/openpanel"].startswith("own-dir")
    assert decision.selected["platform/alerting"].startswith("declared dep")
    # libs/ matched alerting's declared dep, so only the truly-ownerless file drops
    assert decision.dropped == ["docs/notes.md"]


def test_explain_fanout_agrees_with_match_changed_services():
    files = ["platform/24.openpanel/compose.yaml", "libs/x.py"]
    manifest = {"platform/alerting": ["libs/**"]}
    assert set(
        explain_fanout(files, manifest=manifest).selected
    ) == match_changed_services(files, manifest=manifest)


def test_dockerfile_baked_shared_trees():
    dockerfile = (
        "FROM python:3.11-slim\n"
        "COPY platform/12.alerting/app.py /app/app.py\n"  # service's own file, ignored
        "COPY libs /app/libs\n"  # shared tree -> libs
        "ADD ./tools /app/tools\n"  # shared tree -> tools
        "COPY --from=builder /out/bin /usr/bin/bin\n"  # multi-stage, ignored
        "# COPY common /app/common\n"  # comment, ignored
    )
    assert dockerfile_baked_shared_trees(dockerfile) == {"libs", "tools"}


def test_fanout_coverage_violations_flags_undeclared_baked_tree():
    # bakes libs/ but declares nothing -> under-fan-out landmine
    dockerfiles = {"platform/ghost": "FROM x\nCOPY libs /app/libs\n"}
    assert fanout_coverage_violations(dockerfiles, manifest={}) == [
        "platform/ghost: libs"
    ]
    # declaring the tree clears the violation
    assert (
        fanout_coverage_violations(
            dockerfiles, manifest={"platform/ghost": ["libs/**"]}
        )
        == []
    )


def test_shipped_manifest_has_no_fanout_coverage_violations():
    # The real alerting Dockerfile bakes libs/+tools/ and the shipped manifest
    # declares them; this locks the repo against regressing that coverage.
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    dockerfiles = {}
    for service_root in ("platform", "finance_report", "bootstrap"):
        base = root / service_root
        if not base.is_dir():
            continue
        for df in base.rglob("Dockerfile*"):
            key = service_key_from_path(df.relative_to(root).as_posix())
            if key:
                dockerfiles[key] = df.read_text(encoding="utf-8", errors="replace")
    assert fanout_coverage_violations(dockerfiles) == []
