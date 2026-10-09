"""What judges a pull request is computed, and must never compute to less.

`SELF_GOVERNING_FILES` was a hand-written list of five paths. A hand-written
list is open: `tools/pr_merge_gate.py` imports `tools/omca_gate_policy.py` (the
blocking audit layer) and `libs/console.py`, and neither was on it. Adding an
import widened what judges a pull request without widening what protects it.

Replacing it with a closure fixes that by construction and introduces the
opposite risk — a computation can quietly return less than the list did, and
an empty or shrunken protected set is a silent loosening of exactly the rule
it implements. Hence the historical list is pinned here.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from libs.gate.self_governance import PACKAGE_FILES
from libs.gate.types import DEFAULT_REPO
from tools import pr_merge_gate as gate

ROOT = pathlib.Path(__file__).resolve().parents[2]

# The five paths the hand-written list held, verbatim. This is a ratchet, not
# a description: the closure may grow past it, never behind it.
LIST_BEFORE_THE_CLOSURE = frozenset(
    {
        "tools/pr_merge_gate.py",
        "libs/tests/test_pr_merge_gate.py",
        "docs/ssot/ops.merge-gate.md",
        "docs/ssot/ci-gate-inventory.yaml",
        "AGENTS.md",
    }
)


def test_the_closure_still_covers_everything_the_list_did() -> None:
    lost = sorted(LIST_BEFORE_THE_CLOSURE - gate.self_governing_files())
    assert not lost, (
        f"the computed set no longer protects {lost}, which the hand-written "
        "list did — a change to those would now be judged by the version it "
        "introduces"
    )


def test_the_closure_found_what_the_list_had_missed() -> None:
    """The reason for computing it, asserted rather than described."""
    found = gate.self_governing_files()
    # pr_merge_gate imports omca_gate_policy, which can block a merge.
    assert "tools/omca_gate_policy.py" in found
    assert "libs/console.py" in found


def test_an_import_reaches_the_protected_set(tmp_path) -> None:
    """The property that makes the closure a ratchet: importing something new
    protects it, with nobody having to remember."""
    module = "libs/console.py"
    assert module in gate._repo_deps("tools/pr_merge_gate.py"), (
        "the dependency walk stopped finding a module this file imports; the "
        "closure is no longer following imports"
    )


def test_rule_texts_are_listed_because_no_code_reads_them() -> None:
    """A closure over code cannot reach a file no code opens.

    Both rule texts appear in `pr_merge_gate.py` only inside comments, and the
    walk deliberately does not follow prose — following it would sweep in
    every path ever mentioned in a docstring.
    """
    source = (ROOT / "tools/pr_merge_gate.py").read_text(encoding="utf-8")
    for rel in gate.RULE_TEXT_FILES:
        assert rel in gate.self_governing_files()
        assert (
            f'"{rel}"'
            not in source.replace(f'RULE_TEXT_FILES = ("AGENTS.md", "{rel}")', "")
            or rel == "AGENTS.md"
        ), f"{rel} is now read by code; compute it instead"


def test_the_set_is_never_empty() -> None:
    """An empty protected set would let every rewrite through — the one
    outcome worse than a wrong set."""
    found = gate.self_governing_files()
    assert found
    assert "tools/pr_merge_gate.py" in found


def test_every_member_exists() -> None:
    """A stale entry protects nothing and reads as though it does. The one exception
    is a package file that a PR could add: it is protected before it exists."""
    missing = sorted(f for f in gate.self_governing_files() if not (ROOT / f).is_file())
    stale = sorted(set(missing) - set(PACKAGE_FILES))
    assert not stale, f"protected paths that do not exist: {stale}"


def test_the_guard_of_the_production_lock_is_in_the_closure() -> None:
    """With the lock, the gate stops sending workflow changes to the owner (#1138).
    The code that reads the lock, the contract tables and their tests then decide
    that, so a change to them must stay with the owner."""
    found = gate.self_governing_files()
    for path in (
        "libs/gate/production_lock.py",
        "libs/gate/production_contract.py",
        "libs/tests/test_production_lock.py",
        # Outside the test_<stem>.py rule; the closure names it explicitly.
        "libs/tests/test_production_environment_gate.py",
    ):
        assert path in found, path


@pytest.mark.parametrize(
    "path",
    [
        "libs/__init__.py",
        "libs/gate/__init__.py",
        "libs/tests/__init__.py",
        "tools/__init__.py",
        "libs/tests/test_gate_pipeline.py",
        "libs/tests/test_gate_self_governance.py",
    ],
)
def test_package_files_and_the_gate_tests_are_in_the_closure(path) -> None:
    """Python runs each parent `__init__.py` before the gate code (#1138)."""
    assert path in gate.self_governing_files()
    assert gate.is_self_governing(path)


def test_a_package_file_that_does_not_exist_yet_is_still_protected() -> None:
    """`python -m tools.pr_merge_gate` would run a `tools/__init__.py` that a PR adds."""
    assert not (ROOT / "tools/__init__.py").exists(), "premise: no tools/__init__.py"
    assert gate.is_self_governing("tools/__init__.py")


def _base_tree(skip: str = "", extra: dict[str, str] | None = None):
    tree = [
        {"path": f, "sha": gate._blob_sha((ROOT / f).read_bytes())}
        for f in sorted(gate.self_governing_files())
        if (ROOT / f).is_file() and f != skip
    ] + [{"path": k, "sha": v} for k, v in (extra or {}).items()]
    payload = json.dumps({"truncated": False, "tree": tree})
    return lambda argv: payload


def test_a_package_file_absent_here_and_in_the_base_is_no_drift() -> None:
    assert gate._working_tree_rule_drift(DEFAULT_REPO, "main", gh=_base_tree()) == ()


def test_a_package_file_the_base_has_and_this_checkout_lacks_is_drift() -> None:
    gh = _base_tree(extra={"tools/__init__.py": "0" * 40})
    drift = gate._working_tree_rule_drift(DEFAULT_REPO, "main", gh=gh)
    assert drift == ("tools/__init__.py",)


def test_an_existing_member_missing_from_the_base_is_still_drift() -> None:
    gh = _base_tree(skip="libs/gate/__init__.py")
    drift = gate._working_tree_rule_drift(DEFAULT_REPO, "main", gh=gh)
    assert drift == ("libs/gate/__init__.py",)


SHADOWS = (
    "tools/pr_merge_gate/__init__.py",  # a package beats tools/pr_merge_gate.py
    "tools/pr_merge_gate/__main__.py",
    "yaml.py",  # a root module beats the installed PyYAML
    "yaml/__init__.py",  # so does a root package
    "libs/gate/types/__init__.py",
    "libs/gate/yaml.py",  # a module next to gate code, named like an import
    "libs/gate/types.abi3.so",  # an extension module beats types.py
    "setup.py",
    "conftest.py",
    "libs/tests/conftest.py",
    "libs/tests/fixtures/conftest.py",
    "libs/conftest.py",
)


@pytest.mark.parametrize("path", SHADOWS)
def test_a_file_that_shadows_the_gate_is_self_governing(path) -> None:
    """#1138 re-audit H2 and M3: each of these runs instead of, or before, gate code."""
    assert gate.is_self_governing(path)


@pytest.mark.parametrize(
    "path",
    [
        "tools/new_tool.py",
        "libs/probe_specs.py",
        "docs/x.py",
        "e2e_regressions/conftest.py",
        "tools/deploy_v2.py",
    ],
)
def test_an_unrelated_file_is_not_self_governing(path) -> None:
    assert not gate.is_self_governing(path)


def test_the_gate_tests_conftest_is_in_the_closure() -> None:
    """`collect_ignore_glob` in it would switch off the gate tests (#1138 M3)."""
    assert (ROOT / "libs/tests/conftest.py").is_file()
    assert "libs/tests/conftest.py" in gate.self_governing_files()


# --- #1138 round 3 R3-2: case variants, tooling paths, and tool configuration --------


@pytest.mark.parametrize(
    "path",
    [
        "Tools/pr_merge_gate.py",
        "LIBS/Gate/Evaluator.py",
        ".github/Workflows/infra-ci.yml",
        "tools\\pr_merge_gate.py",
        "./tools/pr_merge_gate.py",
        "libs/gate/../gate/evaluator.py",
        "Tools/PR_Merge_Gate/__init__.py",
    ],
)
def test_another_spelling_of_a_gate_path_is_self_governing(path) -> None:
    """The gate host's disk does not tell case apart (#1138 round 3)."""
    assert gate.is_self_governing(path)


@pytest.mark.parametrize(
    "path",
    [
        "libs/gate/__pycache__/evaluator.cpython-312.pyc",
        ".venv/lib/python3.12/site-packages/yaml/__init__.py",
        "venv/lib/x.py",
        "docs/site-packages/x.txt",
        "libs/deploy/x.pyc",
        "libs/deploy/x.pyd",
        "libs/deploy/x.pth",
        "libs/deploy/x.so",
        "libs/deploy/x.abi3.so",
        "libs/deploy/x.cpython-312-darwin.so",
    ],
)
def test_compiled_files_and_virtual_environments_are_self_governing(path) -> None:
    assert gate.is_self_governing(path)


@pytest.mark.parametrize(
    "path",
    [
        "pytest.ini",
        "libs/pytest.ini",
        "libs/tests/pytest.ini",
        "tox.ini",
        "libs/tests/tox.ini",
        "setup.cfg",
        "LIBS/Setup.cfg",
        ".coveragerc",
        "libs/sitecustomize.py",
        "tools/usercustomize.py",
        "docs/deep/sitecustomize.py",
    ],
)
def test_pytest_and_site_configuration_is_self_governing(path) -> None:
    assert gate.is_self_governing(path)


def test_pyproject_toml_stays_outside_the_gate() -> None:
    """Dependency edits are common; the docs name this residual (#1147)."""
    assert not gate.is_self_governing("pyproject.toml")


# Thirty tracked paths, chosen across docs, tools, libs, platform and bootstrap.
ORDINARY_PATHS = (
    "docs/onboarding/01.quick-start.md",
    "docs/project/Infra-006.TODOWRITE.md",
    "docs/project/Infra-020.TODOWRITE.md",
    "docs/ssot/bootstrap.dns_and_cert.md",
    "docs/ssot/db.clickhouse.md",
    "docs/ssot/ops.pipeline.md",
    "tools/app_compose_id_drift.py",
    "tools/ci_gate_ruleset_audit.py",
    "tools/env_tool.py",
    "tools/lint_compose_resource_limits.py",
    "tools/promotion_soak_guard.py",
    "tools/service_identity_audit.py",
    "libs/alerting/__init__.py",
    "libs/common.py",
    "libs/core/harness/sweep.py",
    "libs/deploy/dependencies.py",
    "libs/deploy/git_provenance.py",
    "libs/deploy/release_markers.py",
    "libs/observability/openpanel.py",
    "libs/observability/watchers/__init__.py",
    "platform/01.postgres/.env.example",
    "platform/03.clickhouse/config.xml",
    "platform/10.authentik/shared_tasks.py",
    "platform/21.portal/compose.yaml",
    "platform/24.openpanel/shared_tasks.py",
    "bootstrap/.env.production.example",
    "bootstrap/01.dokploy_install/host_guard/infra2-host-heartbeat.timer",
    "bootstrap/03.dokploy_setup/README.md",
    "bootstrap/05.vault/README.md",
    "bootstrap/06.iac_runner/env.manifest.json",
)


def test_ordinary_tracked_paths_are_not_self_governing() -> None:
    """The rules must not send every PR to the owner."""
    assert len(ORDINARY_PATHS) == 30
    missing = [p for p in ORDINARY_PATHS if not (ROOT / p).is_file()]
    assert not missing, f"pick other tracked paths: {missing}"
    governed = [p for p in ORDINARY_PATHS if gate.is_self_governing(p)]
    assert not governed, governed


@pytest.mark.parametrize(
    "path",
    [
        "e2e_regressions/pytest.ini",
        "docs/setup.cfg",
        "libs/deploy/tox.ini",
        "libs/tests/fixtures/.coveragerc",
    ],
)
def test_pytest_settings_off_the_way_to_the_gate_tests_are_not_held(path) -> None:
    """#1138 round 4: pytest reads settings for libs/tests only from the root,
    libs/ and libs/tests/."""
    assert not gate.is_self_governing(path)


@pytest.mark.parametrize(
    "path", [".venv/pyvenv.cfg", "libs/gate/__pycache__/note.txt", "venv/x.txt"]
)
def test_each_tooling_directory_name_counts(path) -> None:
    """#1138 round 4: each name in the rule has a path that only it catches."""
    assert gate.is_self_governing(path)


def test_the_required_check_report_test_is_in_the_closure() -> None:
    """It proves that a required check can report on any PR, so a change to it
    changes what the gate trusts (#1138 round 4)."""
    path = "libs/tests/test_required_checks_can_report.py"
    assert (ROOT / path).is_file()
    assert path in gate.self_governing_files()
    assert gate.is_self_governing(path)
