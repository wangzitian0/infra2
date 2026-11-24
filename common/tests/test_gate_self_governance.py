"""What judges a pull request is computed, and must never compute to less.

`SELF_GOVERNING_FILES` was a hand-written list of five paths. A hand-written
list is open: `tools/pr_merge_gate.py` imports `tools/omca_gate_policy.py` (the
blocking audit layer) and `common/console.py`, and neither was on it. Adding an
import widened what judges a pull request without widening what protects it.

Replacing it with a closure fixes that by construction and introduces the
opposite risk — a computation can quietly return less than the list did, and
an empty or shrunken protected set is a silent loosening of exactly the rule
it implements. Hence the historical list is pinned here.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from common.gate.self_governance import PACKAGE_FILES
from common.gate.types import DEFAULT_REPO
from common.gate import pr_merge_gate as gate

ROOT = pathlib.Path(__file__).resolve().parents[2]

# The five paths the hand-written list held, verbatim. This is a ratchet, not
# a description: the closure may grow past it, never behind it.
LIST_BEFORE_THE_CLOSURE = frozenset(
    {
        "tools/pr_merge_gate.py",
        "common/tests/test_pr_merge_gate.py",
        "docs/ssot/ops.merge-gate.md",
        "docs/ssot/ci-gate-inventory.yaml",
        "AGENTS.md",
    }
)


MUST_BE_GOVERNED = (
    # the hand-written list the closure replaced (a ratchet: grow, never shrink)
    *sorted(LIST_BEFORE_THE_CLOSURE),
    # the closure found what the list had missed
    "common/gate/omca_policy.py",
    "common/console.py",
    # entry point, logic module and the one test of the logic (#1823 Step 4.3)
    "tools/pr_merge_gate.py",
    "common/gate/pr_merge_gate.py",
    "common/tests/test_pr_merge_gate.py",
    # the production lock guard (#1138); the last file is outside test_<stem>.py
    "common/gate/production_lock.py",
    "common/gate/production_contract.py",
    "common/tests/test_production_lock.py",
    "common/tests/test_production_environment_gate.py",
    # package files run before gate code (#1138) and the gate tests
    "common/__init__.py",
    "common/gate/__init__.py",
    "common/tests/__init__.py",
    "tools/__init__.py",
    "common/tests/test_gate_pipeline.py",
    "common/tests/test_gate_self_governance.py",
    "common/tests/test_gate_yaml_parse_cache.py",
    "common/tests/conftest.py",  # collect_ignore_glob could switch off the gate tests
    "common/tests/test_required_checks_can_report.py",
    # scheduled automation run by ops-checks.yml (#1127 / #1147, #1823 Step 4.3)
    "tools/host_hygiene_schedule.py",
    "tools/facet_reconcile.py",
    "common/deploy/host_hygiene_schedule.py",
    "common/deploy/facet_reconcile.py",
    "common/tests/test_host_hygiene_schedule.py",
    "common/tests/test_facet_reconcile.py",
    # the replay module is gate code (#1653)
    "common/gate/replay.py",
    "common/tests/test_gate_replay.py",
    # rule texts: no code opens them, so the closure lists them
    *gate.RULE_TEXT_FILES,
)


def test_the_closure_holds_every_path_that_judges_a_pull_request() -> None:
    found = gate.self_governing_files()
    assert found and len(MUST_BE_GOVERNED) > 30  # an empty set loosens every rule
    lost = sorted(p for p in MUST_BE_GOVERNED if p not in found)
    assert not lost, f"the computed set no longer protects {lost}"
    unjudged = sorted(p for p in MUST_BE_GOVERNED if not gate.is_self_governing(p))
    assert not unjudged, unjudged


def test_the_dependency_walk_follows_imports() -> None:
    """Importing something new protects it, with nobody having to remember."""
    assert "common/console.py" in gate._repo_deps("common/gate/pr_merge_gate.py")
    # The CLI entry point holds no logic; the walk must reach the module that does.
    assert "common/gate/pr_merge_gate.py" in gate._repo_deps("tools/pr_merge_gate.py")


def test_every_member_exists() -> None:
    """A stale entry protects nothing and reads as though it does. The one exception
    is a package file that a PR could add: it is protected before it exists."""
    missing = sorted(f for f in gate.self_governing_files() if not (ROOT / f).is_file())
    stale = sorted(set(missing) - set(PACKAGE_FILES))
    assert not stale, f"protected paths that do not exist: {stale}"
    assert not (ROOT / "tools/__init__.py").exists(), "premise: no tools/__init__.py"
    assert "tools/__init__.py" in gate.self_governing_files()


def _base_tree(skip: str = "", extra: dict[str, str] | None = None):
    tree = [
        {"path": f, "sha": gate._blob_sha((ROOT / f).read_bytes())}
        for f in sorted(gate.self_governing_files())
        if (ROOT / f).is_file() and f != skip
    ] + [{"path": k, "sha": v} for k, v in (extra or {}).items()]
    payload = json.dumps({"truncated": False, "tree": tree})
    return lambda argv: payload


def test_rule_drift_against_the_base_tree() -> None:
    # absent here and in the base: no drift
    assert gate._working_tree_rule_drift(DEFAULT_REPO, "main", gh=_base_tree()) == ()
    # the base has a package file this checkout lacks: drift
    gh = _base_tree(extra={"tools/__init__.py": "0" * 40})
    assert gate._working_tree_rule_drift(DEFAULT_REPO, "main", gh=gh) == (
        "tools/__init__.py",
    )
    # an existing member missing from the base: drift
    gh = _base_tree(skip="common/gate/__init__.py")
    assert gate._working_tree_rule_drift(DEFAULT_REPO, "main", gh=gh) == (
        "common/gate/__init__.py",
    )


SHADOWS = (
    "tools/pr_merge_gate/__init__.py",  # a package beats tools/pr_merge_gate.py
    "tools/pr_merge_gate/__main__.py",
    "yaml.py",  # a root module beats the installed PyYAML
    "yaml/__init__.py",  # so does a root package
    "common/gate/types/__init__.py",
    "common/gate/yaml.py",  # a module next to gate code, named like an import
    "common/gate/types.abi3.so",  # an extension module beats types.py
    "setup.py",
    "conftest.py",
    "common/tests/conftest.py",
    "common/tests/fixtures/conftest.py",
    "common/conftest.py",
)


GOVERNED_SPELLINGS = (
    # case variants and path tricks: the gate host's disk does not tell case apart
    "Tools/pr_merge_gate.py",
    "COMMON/Gate/Evaluator.py",
    ".github/Workflows/infra-ci.yml",
    "tools\\pr_merge_gate.py",
    "./tools/pr_merge_gate.py",
    "common/gate/../gate/evaluator.py",
    "Tools/PR_Merge_Gate/__init__.py",
    # compiled files and virtual environments
    "common/gate/__pycache__/evaluator.cpython-312.pyc",
    ".venv/lib/python3.12/site-packages/yaml/__init__.py",
    "venv/lib/x.py",
    "docs/site-packages/x.txt",
    "common/deploy/x.pyc",
    "common/deploy/x.pyd",
    "common/deploy/x.pth",
    "common/deploy/x.so",
    "common/deploy/x.abi3.so",
    "common/deploy/x.cpython-312-darwin.so",
    ".venv/pyvenv.cfg",
    "common/gate/__pycache__/note.txt",
    "venv/x.txt",
    # pytest and site configuration
    "pytest.ini",
    "common/pytest.ini",
    "common/tests/pytest.ini",
    "tox.ini",
    "common/tests/tox.ini",
    "setup.cfg",
    "COMMON/Setup.cfg",
    ".coveragerc",
    "common/sitecustomize.py",
    "tools/usercustomize.py",
    "docs/deep/sitecustomize.py",
    "pyproject.toml",  # carries pytest settings (1035-R4 / #1147)
)

UNRELATED = (
    "tools/new_tool.py",
    "common/probe_specs.py",
    "docs/x.py",
    "e2e_regressions/conftest.py",
    "tools/deploy_v2.py",
    "docs/pyproject.toml",
    # pytest reads settings for common/tests only from the root, common/, common/tests/
    "e2e_regressions/pytest.ini",
    "docs/setup.cfg",
    "common/deploy/tox.ini",
    "common/tests/fixtures/.coveragerc",
)


def test_a_path_that_shadows_or_configures_the_gate_is_self_governing() -> None:
    """#1138 re-audit H2, M3, round 3 and round 4: each runs instead of, or before, gate code."""
    paths = SHADOWS + GOVERNED_SPELLINGS
    assert len(set(paths)) == len(paths)
    loose = [p for p in paths if not gate.is_self_governing(p)]
    assert not loose, loose


# Thirty tracked paths, chosen across docs, tools, common, platform and bootstrap.
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
    "tools/promotion_reminder.py",
    "tools/service_identity_audit.py",
    "common/alerting/__init__.py",
    "common/common.py",
    "common/core/harness/sweep.py",
    "common/deploy/dependencies.py",
    "common/deploy/v2.py",
    "common/deploy/app_contract.py",
    "common/observability/openpanel.py",
    "common/observability/watchers/__init__.py",
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


def test_an_unrelated_path_is_not_self_governing() -> None:
    """The rules must not send every PR to the owner."""
    assert len(ORDINARY_PATHS) == 30
    missing = [p for p in ORDINARY_PATHS if not (ROOT / p).is_file()]
    assert not missing, f"pick other tracked paths: {missing}"
    governed = [p for p in (*UNRELATED, *ORDINARY_PATHS) if gate.is_self_governing(p)]
    assert not governed, governed


def test_every_repository_module_the_gate_runs_is_in_the_closure() -> None:
    """A module that `python -m tools.pr_merge_gate` executes but the closure omits
    could change a verdict without the review set (#1602 review: importing
    `common.core` ran `common/core/constants.py` and `environ.py` in the gate)."""
    probe = (
        "import json, os, sys\n"
        "import tools.pr_merge_gate\n"
        "root = os.getcwd()\n"
        "files = sorted({os.path.relpath(m.__file__, root) for m in list(sys.modules.values())\n"
        "    if getattr(m, '__file__', None) and m.__file__.startswith(root + os.sep)\n"
        "    and '.venv' not in m.__file__})\n"
        "print(json.dumps(files))\n"
    )
    env = {
        k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")
    }
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    loaded = [p.replace(os.sep, "/") for p in json.loads(out)]
    assert "tools/pr_merge_gate.py" in loaded and len(loaded) >= 5, loaded
    closure = set(gate.self_governing_files())
    outside = [p for p in loaded if p not in closure]
    assert not outside, f"the gate runs these files outside its closure: {outside}"
