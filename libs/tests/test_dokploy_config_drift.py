"""Tests for the T3 config-drift reconciler's pure parts (tools/dokploy_config_drift.py).

The live halves (Dokploy API, git-at-tag deployer loading) run in
the facet-reconcile job (#542) with --self-check; here we lock the pure logic a
silent regression would hide behind: report formatting must surface DRIFT and
ERROR loudly (a "0 drift" that silently skipped N services is the lie this
tool exists to avoid), and contents_at_ref must distinguish missing paths.
"""

from __future__ import annotations

import subprocess

import pytest

import tools.dokploy_config_drift as drift
from tools.dokploy_config_drift import (
    DeployedIdentity,
    Row,
    contents_at_ref,
    format_report,
    strict_blockers,
)


def test_format_report_all_in_sync_says_green() -> None:
    rows = [Row(service="platform/postgres", verdict="in_sync")]

    report = format_report("v1.1.19", rows)

    assert "in sync 1 · DRIFT 0" in report
    assert "✅ every comparable service matches production target v1.1.19." in report


def test_format_report_surfaces_drift_with_both_hashes() -> None:
    rows = [
        Row(
            service="platform/redis",
            verdict="DRIFT",
            expected="abc123",
            deployed="def456",
        ),
        Row(service="platform/postgres", verdict="in_sync"),
    ]

    report = format_report("v1.1.19", rows)

    assert "🔴 DRIFT platform/redis" in report
    assert "expected=abc123" in report and "deployed=def456" in report
    assert "✅" not in report  # a drifted run must not read as healthy


def test_format_report_surfaces_errors_loudly() -> None:
    """A service the tool could not check must appear as ERROR, never be
    silently folded into a healthy-looking summary."""
    rows = [
        Row(
            service="platform/authentik", verdict="error", note="deployer import failed"
        ),
        Row(service="platform/postgres", verdict="in_sync"),
    ]

    report = format_report("v1.1.19", rows)

    assert "⚠️ ERROR platform/authentik" in report
    assert "deployer import failed" in report
    assert "error 1" in report


def test_format_report_classifies_non_comparable_rows() -> None:
    rows = [
        Row(service="platform/portal", verdict="not_deployed"),
        Row(service="platform/signoz", verdict="structural", note="compose renamed"),
        Row(service="platform/minio", verdict="env_unavailable"),
    ]

    report = format_report("v1.1.19", rows)

    assert "not deployed: platform/portal" in report
    assert "structural: platform/signoz (compose renamed)" in report
    assert "env-skip: platform/minio" in report


def test_contents_at_ref_reads_tracked_file_and_omits_missing() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    out = contents_at_ref(head, ["pyproject.toml", "does/not/exist.txt"])

    assert b'name = "infra2"' in out["pyproject.toml"]
    assert "does/not/exist.txt" not in out  # missing at ref -> omitted, caller detects


def test_contents_at_ref_empty_paths_is_noop() -> None:
    assert contents_at_ref("HEAD", []) == {}


# Captured before the autouse fixture below replaces the module attribute.
_REAL_HASHES_WITH_REF_CODE = drift.hashes_with_ref_code


@pytest.fixture(autouse=True)
def _no_release_checkout(monkeypatch):
    """Unit tests never check out a release; a test that needs one overrides this."""

    def unavailable(ref, service_ids, *, root=None):
        raise RuntimeError("no release checkout in unit tests")

    monkeypatch.setattr(drift, "hashes_with_ref_code", unavailable)


def _stub_single_service_scan(monkeypatch, identity, hashes_by_ref):
    class DummyDeployer:
        pass

    monkeypatch.setattr(
        drift.service_registry, "all_services", lambda: ["platform/example"]
    )
    monkeypatch.setattr(drift, "_load_deployer", lambda _sid: DummyDeployer)
    monkeypatch.setattr(
        drift, "_deployed_identities", lambda: {"platform/example": identity}
    )
    monkeypatch.setattr(drift, "_commit_at_ref", lambda _tag: "a" * 40)
    monkeypatch.setattr(drift, "_source_env_vars", lambda _dep: {"ENV": "production"})
    monkeypatch.setattr(
        drift,
        "expected_hash_at",
        lambda _dep, _context, ref, _env: (hashes_by_ref[ref], []),
    )
    return drift.scan("v1.2.3")[0]


def test_scan_accepts_older_deploy_ref_when_source_identity_is_unchanged(
    monkeypatch,
) -> None:
    old_ref = "b" * 40
    row = _stub_single_service_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="runtime",
            source_hash="v1:same",
            deploy_ref=old_ref,
        ),
        {"v1.2.3": "same", old_ref: "same"},
    )

    assert row.verdict == "in_sync"
    assert row.deployed_ref == old_ref


def test_scan_detects_source_identity_that_does_not_match_its_own_ref(
    monkeypatch,
) -> None:
    old_ref = "b" * 40
    row = _stub_single_service_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="runtime",
            source_hash="v1:current",
            deploy_ref=old_ref,
        ),
        {"v1.2.3": "current", old_ref: "different"},
    )

    assert row.verdict == "DRIFT"
    assert "does not match its deployed ref" in row.note


def test_scan_classifies_pre_migration_identity_without_false_drift(
    monkeypatch,
) -> None:
    row = _stub_single_service_scan(
        monkeypatch,
        DeployedIdentity(runtime_hash="same"),
        {"v1.2.3": "same"},
    )

    assert row.verdict == "legacy_identity"
    assert not strict_blockers([row])


def test_load_deployer_is_consolidated_onto_the_shared_helper() -> None:
    """This module's own _load_deployer used to be an independent, hand-copied
    duplicate that hardcoded "platform" vs "finance_report/finance_report" as
    the only two possible base paths — silently mis-resolving (or never
    reaching) every truealpha/* service_id since #500. Reusing the shared,
    already-tested libs.deploy.deployer.load_deployer_class (built for exactly
    this job) is the fix; this locks the consolidation so a future edit can't
    quietly regrow a second copy of the same bug."""
    from libs.deploy.deployer import load_deployer_class

    assert drift._load_deployer is load_deployer_class


class _FakeDokployClientForIdentities:
    """Enough of DokployClient's shape for _deployed_identities: list_projects()
    + _request() for a compose.one lookup. No real credentials/network."""

    def __init__(self) -> None:
        pass

    def list_projects(self):
        return [
            {
                "name": "truealpha",
                "environments": [
                    {
                        "name": "production",
                        "compose": [{"name": "app", "composeId": "cid-ta-app"}],
                    },
                    {
                        "name": "staging",
                        "compose": [{"name": "app", "composeId": "cid-ta-app-staging"}],
                    },
                ],
            },
            {
                # A Dokploy project outside infra2's managed layers (e.g. an
                # unrelated personal project on the same instance) must stay
                # excluded — the fix derives the allowlist from
                # service_registry._LAYERS, it does not just accept everything.
                "name": "someone-elses-project",
                "environments": [
                    {
                        "name": "production",
                        "compose": [{"name": "widget", "composeId": "cid-widget"}],
                    }
                ],
            },
        ]

    def _request(self, method: str, path: str):
        assert method == "GET"
        assert path == "compose.one?composeId=cid-ta-app"
        return {"env": "IAC_CONFIG_HASH=deploy-v0.0.11-1785146688203\n"}


def test_deployed_identities_includes_truealpha_and_excludes_unmanaged_projects(
    monkeypatch,
) -> None:
    """#500 onboarded truealpha as a real deploy_v2 layer, but
    _deployed_identities()'s project filter was never updated past its
    original ("platform", "finance_report") tuple — every truealpha/* service
    silently read as "not deployed" no matter what was actually live. The fix
    derives the allowlist from service_registry._LAYERS instead."""
    monkeypatch.setattr(
        "libs.deploy.dokploy_client.DokployClient", _FakeDokployClientForIdentities
    )

    identities = drift._deployed_identities()

    assert "truealpha/app" in identities
    assert identities["truealpha/app"].runtime_hash == "deploy-v0.0.11-1785146688203"
    assert not any(key.startswith("someone-elses-project/") for key in identities)


def test_strict_blockers_fail_on_detector_and_structural_errors() -> None:
    rows = [
        Row("platform/a", "in_sync"),
        Row("platform/b", "DRIFT"),
        Row("platform/c", "error"),
        Row("platform/d", "structural"),
        Row("platform/e", "legacy_identity"),
    ]

    assert [row.service for row in strict_blockers(rows)] == [
        "platform/b",
        "platform/c",
        "platform/d",
    ]


def test_production_target_prefers_explicit_marker(monkeypatch) -> None:
    monkeypatch.setattr(
        drift,
        "_tags",
        lambda pattern: {
            "production/v*.*.*": ["production/v1.1.48"],
            "v*.*.*": ["v1.1.52"],
        }[pattern],
    )

    assert drift._production_target_tag() == "production/v1.1.48"


def test_production_target_fails_closed_without_marker(monkeypatch) -> None:
    monkeypatch.setattr(drift, "_tags", lambda _pattern: [])

    with pytest.raises(SystemExit, match="desired state is unknown"):
        drift._production_target_tag()


def test_production_target_fails_closed_on_malformed_marker(monkeypatch) -> None:
    monkeypatch.setattr(drift, "_tags", lambda _pattern: ["production/v1.1.53-rc.1"])

    with pytest.raises(SystemExit, match="invalid production marker"):
        drift._production_target_tag()


def test_every_runtime_only_deployer_has_a_secret_free_source_contract() -> None:
    environment = {
        "ENV": "production",
        "ENV_SUFFIX": "",
        "ENV_DOMAIN_SUFFIX": "",
        "INTERNAL_DOMAIN": "example.test",
    }
    checked = []
    for service_id in drift.service_registry.all_services():
        deployer = drift._load_deployer(service_id)
        if deployer is None or not deployer.runtime_only_config_keys:
            continue
        source = deployer.source_config_env_base(environment)
        assert not deployer.runtime_only_config_keys.intersection(source), service_id
        checked.append(service_id)

    assert checked == ["platform/alerting", "truealpha/data_engine"]


def test_an_input_added_after_the_release_is_not_a_structural_finding(
    monkeypatch, tmp_path
) -> None:
    """2026-09-15: tools/pr_merge_gate.py merged an hour after production/v1.1.82 was
    promoted; it matches platform/alerting's `tools/**` dependency, so the daily reconcile
    reported `structural: 4 input(s) absent at production/v1.1.82` with zero drift. The
    deploy at the release never hashed a file that did not exist yet."""
    compose = "services:\n  x:\n    image: y\n"
    newer = tmp_path / "libs" / "newer.py"
    newer.parent.mkdir(parents=True)
    newer.write_text("added after the tag\n", encoding="utf-8")

    class Dep:
        compose_path = "platform/x/compose.yaml"

    monkeypatch.setattr(drift, "ROOT", tmp_path)
    monkeypatch.setattr(
        drift,
        "_hash_input_paths",
        lambda _dep, _c: (
            "platform/x/compose.yaml",
            [],
            ["libs/newer.py", "libs/gone.py"],
        ),
    )
    monkeypatch.setattr(
        drift,
        "contents_at_ref",
        lambda _ref, paths: {"platform/x/compose.yaml": compose.encode()},
    )
    expected, missing = drift.expected_hash_at(
        Dep, None, "production/v1.1.82", {"ENV": "x"}
    )
    # libs/newer.py exists on the tree but not at the ref -> not an input of that release
    # libs/gone.py exists nowhere -> a real structural finding
    assert missing == ["libs/gone.py"]
    assert expected == drift.config_hash_from_items(compose, {"ENV": "x"}, [], [])


def test_scan_resolves_legacy_compose_names(monkeypatch) -> None:
    """Issue #954/#958: When a platform service is renamed (e.g. minio -> s3), production
    Dokploy still holds the legacy identity until Stage 3 promotion. scan() must fall back
    to legacy_compose_names to match deployed state rather than reporting 'not_deployed'."""

    class DummyDeployer:
        project = "platform"
        legacy_compose_names = ("minio",)
        runtime_only_config_keys = frozenset()

        @classmethod
        def source_config_env_base(cls, env):
            return {}

    monkeypatch.setattr(
        drift,
        "_deployed_identities",
        lambda: {
            "platform/minio": DeployedIdentity(
                runtime_hash="hash123",
                source_hash="v1:src123",
                deploy_ref="0" * 40,
            )
        },
    )
    monkeypatch.setattr(drift, "_commit_at_ref", lambda tag: "0" * 40)
    monkeypatch.setattr(drift.service_registry, "all_services", lambda: ["platform/s3"])
    monkeypatch.setattr(
        drift,
        "_load_deployer",
        lambda sid: DummyDeployer if sid == "platform/s3" else None,
    )
    monkeypatch.setattr(drift, "_source_env_vars", lambda dep: {})
    monkeypatch.setattr(
        drift, "expected_hash_at", lambda dep, c, tag, env_vars: ("src123", [])
    )

    rows = drift.scan("v1.2.6")
    assert len(rows) == 1
    assert rows[0].service == "platform/s3"
    assert rows[0].verdict == "in_sync"
    assert rows[0].deployed == "v1:src123"


_STUB = """
import os

SOURCE_CONFIG_HASH_VERSION = "v9"


def _set_env(name):
    pass


def _load_deployer(sid):
    return object


def _source_env_vars(dep):
    return {}


def expected_hash_at(dep, c, ref, env):
    return "@@HASH@@-" + os.environ.get("DOKPLOY_API_KEY", "no-key"), []
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _two_release_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    module = repo / "tools" / "dokploy_config_drift.py"
    module.write_text(_STUB.replace("@@HASH@@", "release-a"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a")
    commit_a = _git(repo, "rev-parse", "HEAD")
    module.write_text(_STUB.replace("@@HASH@@", "release-b"))
    _git(repo, "commit", "-q", "-am", "b")
    commit_b = _git(repo, "rev-parse", "HEAD")
    return repo, commit_a, commit_b


def test_hashes_with_ref_code_runs_the_code_of_that_ref(tmp_path, monkeypatch) -> None:
    """#1071: the expected identity of a release comes from that release's code.
    Two commits carry two different formulas; each ref must answer with its own,
    without the parent's credentials, and leave no worktree behind."""
    repo, commit_a, commit_b = _two_release_repo(tmp_path)
    monkeypatch.setenv("DOKPLOY_API_KEY", "secret-value")

    a = _REAL_HASHES_WITH_REF_CODE(commit_a, ["platform/x"], root=repo)
    b = _REAL_HASHES_WITH_REF_CODE(commit_b, ["platform/x"], root=repo)

    assert a == {
        "version": "v9",
        "services": {"platform/x": {"hash": "release-a-no-key", "missing": []}},
    }
    assert b["services"]["platform/x"]["hash"] == "release-b-no-key"
    assert len(_git(repo, "worktree", "list").splitlines()) == 1


def test_hashes_with_ref_code_refuses_a_commit_off_main(tmp_path) -> None:
    """#1071 audit: a deployed ref can name any fetched commit. Only main's code runs."""
    repo, commit_a, _commit_b = _two_release_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side", commit_a)
    (repo / "tools" / "dokploy_config_drift.py").write_text(
        _STUB.replace("@@HASH@@", "side")
    )
    _git(repo, "commit", "-q", "-am", "side")
    side = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="not on main"):
        _REAL_HASHES_WITH_REF_CODE(side, ["platform/x"], root=repo)


def _release_scan(
    monkeypatch, identity, *, release_by_commit, main_formula, version="v1"
):
    """One service; the tag resolves to commit a*40, the deployed ref to itself."""

    class DummyDeployer:
        pass

    calls = []

    def release(ref, ids, *, root=None):
        calls.append(ref)
        run = release_by_commit[ref]
        if isinstance(run, BaseException):
            raise run
        return {"version": version, "services": {"platform/example": run}}

    monkeypatch.setattr(drift, "hashes_with_ref_code", release)
    monkeypatch.setattr(
        drift.service_registry, "all_services", lambda: ["platform/example"]
    )
    monkeypatch.setattr(drift, "_load_deployer", lambda _sid: DummyDeployer)
    monkeypatch.setattr(
        drift, "_deployed_identities", lambda: {"platform/example": identity}
    )
    monkeypatch.setattr(
        drift, "_commit_at_ref", lambda ref: "a" * 40 if ref == "v1.2.3" else ref
    )
    monkeypatch.setattr(drift, "_source_env_vars", lambda _dep: {})
    monkeypatch.setattr(
        drift, "expected_hash_at", lambda _dep, _c, ref, _env: (main_formula, [])
    )
    return drift.scan("v1.2.3")[0], calls


def test_scan_takes_the_release_formula_over_this_checkout(monkeypatch) -> None:
    """#1071: main's formula disagrees with the deployed identity, the release's own
    code agrees with it. That is not drift."""
    row, _ = _release_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="r", source_hash="v1:released", deploy_ref="b" * 40
        ),
        release_by_commit={
            "a" * 40: {"hash": "released", "missing": []},
            "b" * 40: {"hash": "released", "missing": []},
        },
        main_formula="main-formula",
    )

    assert row.verdict == "in_sync"
    assert row.note == ""


def test_scan_reports_drift_that_only_the_release_code_sees(monkeypatch) -> None:
    """#1071 audit: main's formula would call this in sync; the release's own code at
    the deployed commit does not reproduce the deployed identity."""
    row, _ = _release_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="r", source_hash="v1:hand-edited", deploy_ref="b" * 40
        ),
        release_by_commit={
            "a" * 40: {"hash": "released", "missing": []},
            "b" * 40: {"hash": "released", "missing": []},
        },
        main_formula="hand-edited",
    )

    assert row.verdict == "DRIFT"


def test_a_service_error_in_the_release_run_falls_back_and_says_so(monkeypatch) -> None:
    """#1071 audit: a fallback to this checkout's formula must reach the report."""
    row, _ = _release_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="r", source_hash="v1:released", deploy_ref="b" * 40
        ),
        release_by_commit={
            "a" * 40: {"error": "ImportError: no module named x"},
            "b" * 40: {"error": "ImportError: no module named x"},
        },
        main_formula="main-formula",
    )

    assert row.verdict == "DRIFT"
    assert "this checkout's formula" in row.note and "ImportError" in row.note
    assert "ImportError" in format_report("v1.2.3", [row])


def test_scan_uses_the_hash_version_of_the_release(monkeypatch) -> None:
    """#1071 audit: a version bump on main must not turn every service into an error."""
    row, _ = _release_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="r", source_hash="v7:released", deploy_ref="b" * 40
        ),
        release_by_commit={
            "a" * 40: {"hash": "released", "missing": []},
            "b" * 40: {"hash": "released", "missing": []},
        },
        main_formula="main-formula",
        version="v7",
    )

    assert row.verdict == "in_sync"


def test_a_timed_out_release_run_stops_further_release_runs(monkeypatch) -> None:
    """#1071 audit: two hung runs would use the job's 15 minutes before any report."""
    row, calls = _release_scan(
        monkeypatch,
        DeployedIdentity(
            runtime_hash="r", source_hash="v1:main-formula", deploy_ref="b" * 40
        ),
        release_by_commit={
            "a" * 40: subprocess.TimeoutExpired(cmd="python", timeout=120),
            "b" * 40: {"hash": "main-formula", "missing": []},
        },
        main_formula="main-formula",
    )

    assert calls == ["a" * 40]
    assert row.verdict == "in_sync"
    assert "unavailable" in row.note
