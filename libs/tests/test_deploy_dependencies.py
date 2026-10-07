"""Tests for the deploy dependency graph (fan-out + necessity audit).

Covers #267 / #261 P2: libs/ must NOT fan out to all services, own-dir and
declared deps must select exactly the right services, and the autoDeploy audit
must flag non-allowlisted Dokploy-native triggers.
"""

from libs.deploy.dependencies import (
    autodeploy_violations,
    config_hash_input_count,
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
    # the missing truealpha branch made v1.1.23's reconcile drop
    # truealpha/truealpha/10.app/* changes on the floor (no auto-promotion)
    assert (
        service_key_from_path("truealpha/truealpha/10.app/secrets.ctmpl")
        == "truealpha/app"
    )
    assert (
        service_key_from_path("truealpha/truealpha/01.postgres/compose.yaml")
        == "truealpha/postgres"
    )
    # bootstrap dirs use dashes in the service key
    assert (
        service_key_from_path("bootstrap/01.dokploy_install/x.sh")
        == "bootstrap/dokploy-install"
    )
    # tooling / shared / root paths own no service
    assert service_key_from_path("libs/deploy/deployer.py") is None
    assert service_key_from_path("tools/x.py") is None
    assert service_key_from_path("common/foo.py") is None
    assert service_key_from_path("moon.yml") is None


def test_libs_change_fans_out_to_nothing():
    # The whole point: a shared-tooling change must NOT redeploy every service.
    assert match_changed_services(["libs/deploy/deployer.py"], manifest={}) == set()
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
        ["libs/deploy/deployer.py"], manifest=manifest
    )
    assert "platform/alerting" in match_changed_services(
        ["tools/dokploy_env.py"], manifest=manifest
    )
    # ...but a libs/ change still does NOT fan out to a service that does not
    # bake it in.
    assert "platform/openpanel" not in match_changed_services(
        ["libs/deploy/deployer.py"], manifest=manifest
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
            "libs/deploy/deployer.py",  # declared dep of alerting; drop for openpanel
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


def _service(root, compose: str, dockerfile: str | None = None, files=()):
    """A service directory platform/99.ghost under a temporary repo root."""
    service_dir = root / "platform" / "99.ghost"
    service_dir.mkdir(parents=True)
    (service_dir / "compose.yaml").write_text(compose, encoding="utf-8")
    if dockerfile is not None:
        (service_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return [service_dir / "compose.yaml"]


_REPO_ROOT_CONTEXT = (
    "services:\n"
    "  ghost:\n"
    "    build:\n"
    "      context: ../..\n"
    "      dockerfile: platform/99.ghost/Dockerfile\n"
)


def test_fanout_coverage_flags_an_undeclared_repo_root_file(tmp_path):
    """#1117: `COPY uv.lock` changes the config hash; without a depends_on entry a
    change to uv.lock selects no service. The tree-only guard never looked at it."""
    composes = _service(
        tmp_path,
        _REPO_ROOT_CONTEXT,
        "FROM x\nCOPY uv.lock /tmp/uv.lock\nCOPY platform/99.ghost /app/\n",
        files=("uv.lock", "platform/99.ghost/app.py"),
    )
    assert fanout_coverage_violations(composes, manifest={}, root=tmp_path) == [
        "platform/ghost: uv.lock"
    ]
    declared = {"platform/ghost": ["uv.lock"]}
    assert fanout_coverage_violations(composes, manifest=declared, root=tmp_path) == []


def test_fanout_coverage_flags_an_undeclared_shared_tree(tmp_path):
    composes = _service(
        tmp_path,
        _REPO_ROOT_CONTEXT,
        "FROM x\nCOPY libs /app/libs\nCOPY --from=builder /out /usr/bin/\n",
        files=("libs/a.py", "libs/sub/b.py"),
    )
    assert fanout_coverage_violations(composes, manifest={}, root=tmp_path) == [
        "platform/ghost: libs/a.py",
        "platform/ghost: libs/sub/b.py",
    ]
    declared = {"platform/ghost": ["libs/**"]}
    assert fanout_coverage_violations(composes, manifest=declared, root=tmp_path) == []


def test_fanout_coverage_flags_an_undeclared_bind_mount(tmp_path):
    composes = _service(
        tmp_path,
        "services:\n"
        "  ghost:\n"
        "    image: x\n"
        "    volumes:\n"
        "      - ../../common/conf.yaml:/etc/conf.yaml:ro\n"
        "      - ./own.yaml:/etc/own.yaml:ro\n",
        files=("common/conf.yaml", "platform/99.ghost/own.yaml"),
    )
    assert fanout_coverage_violations(composes, manifest={}, root=tmp_path) == [
        "platform/ghost: common/conf.yaml"
    ]


def test_fanout_coverage_resolves_sources_against_the_build_context(tmp_path):
    """A service whose build context is its own directory may COPY a local `tools/`
    folder; that is own-dir input, not the repo-root tools/ tree."""
    composes = _service(
        tmp_path,
        "services:\n  ghost:\n    build:\n      context: .\n",
        "FROM x\nCOPY tools/run.py /app/\n",
        files=("platform/99.ghost/tools/run.py",),
    )
    assert fanout_coverage_violations(composes, manifest={}, root=tmp_path) == []


def test_fanout_coverage_refuses_an_unreadable_compose(tmp_path):
    import pytest
    import yaml

    composes = _service(tmp_path, "services: [unclosed\n")
    with pytest.raises(yaml.YAMLError):
        fanout_coverage_violations(composes, manifest={}, root=tmp_path)


def test_fanout_coverage_flags_an_input_outside_the_repository(tmp_path):
    """A bind mount that leaves the repository changes the hash, yet no changed-file
    path can name it, so nothing fans out to the service."""
    repo = tmp_path / "repo"
    (tmp_path / "outside.yaml").write_text("x\n", encoding="utf-8")
    composes = _service(
        repo,
        "services:\n"
        "  ghost:\n"
        "    image: x\n"
        "    volumes:\n"
        "      - ../../../outside.yaml:/etc/outside.yaml:ro\n",
    )
    violations = fanout_coverage_violations(composes, manifest={}, root=repo)
    assert len(violations) == 1
    assert violations[0].startswith("platform/ghost: ")
    assert violations[0].endswith("(outside the repository)")


def test_fanout_coverage_flags_an_input_in_another_service_directory(tmp_path):
    """A COPY from a file under another service's directory is not own-directory
    input: a change to it selects the other service, not this one."""
    composes = _service(
        tmp_path,
        _REPO_ROOT_CONTEXT,
        "FROM x\nCOPY platform/98.other/shared.hcl /app/shared.hcl\n",
        files=("platform/98.other/shared.hcl",),
    )
    assert fanout_coverage_violations(composes, manifest={}, root=tmp_path) == [
        "platform/ghost: platform/98.other/shared.hcl"
    ]
    declared = {"platform/ghost": ["platform/98.other/**"]}
    assert fanout_coverage_violations(composes, manifest=declared, root=tmp_path) == []


def test_fanout_coverage_ignores_a_glob_declared_for_another_service(tmp_path):
    """A depends_on glob fans out to the service that declares it, not to every
    service: a COPY of uv.lock stays unguarded when only another service lists it."""
    composes = _service(
        tmp_path,
        _REPO_ROOT_CONTEXT,
        "FROM x\nCOPY uv.lock /tmp/uv.lock\n",
        files=("uv.lock",),
    )
    manifest = {"platform/other": ["uv.lock"]}
    assert fanout_coverage_violations(composes, manifest=manifest, root=tmp_path) == [
        "platform/ghost: uv.lock"
    ]


def test_fanout_coverage_refuses_a_compose_outside_a_service_directory(tmp_path):
    import pytest

    compose = tmp_path / "docs" / "compose.yaml"
    compose.parent.mkdir()
    compose.write_text("services:\n  x:\n    image: y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not in a service directory"):
        fanout_coverage_violations([compose], manifest={}, root=tmp_path)


def test_the_audit_scans_every_service_root():
    """Each service root holds deployed composes (platform, the runner under
    bootstrap, and the two apps). A scan that drops one leaves its inputs unguarded."""
    from tools import deploy_guard_audit

    roots = {
        compose.relative_to(deploy_guard_audit.ROOT).parts[0]
        for compose in deploy_guard_audit.find_service_composes()
    }
    assert {"platform", "bootstrap", "finance_report", "truealpha"} <= roots


def test_config_hash_input_count_counts_every_file_the_hash_reads(tmp_path):
    """Dockerfile, compose file, own-directory source and a repo-root COPY."""
    composes = _service(
        tmp_path,
        _REPO_ROOT_CONTEXT,
        "FROM x\nCOPY uv.lock /tmp/uv.lock\nCOPY platform/99.ghost /app/\n",
        files=("uv.lock", "platform/99.ghost/app.py"),
    )
    assert config_hash_input_count(composes, root=tmp_path) == 4


def test_config_hash_input_count_is_zero_for_a_service_that_reads_no_file(tmp_path):
    composes = _service(tmp_path, "services:\n  ghost:\n    image: x\n")
    assert config_hash_input_count(composes, root=tmp_path) == 0


def test_the_audit_exits_1_and_names_each_violation(monkeypatch, capsys):
    """This exit code is the infra-ci gate and the ops-checks monitor signal."""
    from tools import deploy_guard_audit

    monkeypatch.setattr(
        deploy_guard_audit,
        "fanout_coverage_violations",
        lambda composes: ["platform/ghost: uv.lock"],
    )
    assert deploy_guard_audit.main() == 1
    assert "platform/ghost: uv.lock" in capsys.readouterr().out


def test_the_audit_refuses_a_tree_with_no_service_compose(monkeypatch, capsys):
    """A scan that finds no compose file reads no input. It must fail, not pass."""
    from tools import deploy_guard_audit

    monkeypatch.setattr(deploy_guard_audit, "find_service_composes", lambda: [])
    assert deploy_guard_audit.main() == 1
    assert "no service compose file found" in capsys.readouterr().out


def test_the_audit_refuses_compose_files_that_yield_no_input(monkeypatch, capsys):
    """Composes that exist but yield no config-hash input leave nothing to check."""
    from tools import deploy_guard_audit

    monkeypatch.setattr(deploy_guard_audit, "config_hash_input_count", lambda c: 0)
    assert deploy_guard_audit.main() == 1
    assert "yield no config-hash input" in capsys.readouterr().out


def test_the_audit_reports_how_many_files_it_read(capsys):
    from tools import deploy_guard_audit

    composes = deploy_guard_audit.find_service_composes()
    inputs = config_hash_input_count(composes)
    assert len(composes) > 0 and inputs > 0
    assert deploy_guard_audit.main() == 0
    assert f"({len(composes)} compose files, {inputs} inputs)" in (
        capsys.readouterr().out
    )


def test_shipped_manifest_has_no_fanout_coverage_violations():
    from tools.deploy_guard_audit import audit, find_service_composes

    composes = find_service_composes()
    alerting = [c for c in composes if c.parent.name == "12.alerting"]
    # The audit reads repo-root inputs: alerting COPYs libs/ and tools/ (#267).
    assert fanout_coverage_violations(alerting, manifest={}), (
        "the audit found no repo-root input for alerting, so it checks nothing"
    )
    assert audit() == []
