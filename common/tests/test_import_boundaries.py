"""Import-boundary guard for ``common/`` (infra2#955; #846 acceptance 1, #847).

#846 closed with the acceptance "domain packages do not import flat modules" and nothing
enforcing it: ``test_frozen_shims.py`` checks that a *shim* is pure, but never walks
``common/<domain>/``, so the edges from domain packages back into flat modules (the #955
audit counted 56; two of the targets form import cycles with ``common.core``) went unseen.
The layering ``common/<domain>/`` -> ``common/<flat>.py`` is the wrong way round: flat modules
are the compatibility surface, domain packages are where the implementation lives.

Rules, checked on the AST of every ``common/**/*.py`` outside ``common/tests`` (imports are
never executed, so a module that needs the SDK or a secret is still checked):

1. a file inside a ``common`` domain package must not import a flat ``common/<name>.py``;
2. nothing under ``common/`` may import from ``tools`` (library code must not depend on
   the CLI layer that is built on top of it);
3. nothing under ``common/`` may import from the ``platform`` or ``bootstrap`` Python
   packages (the deployment layers sit above ``common``).

Existing violations are an explicit, **shrink-only** debt ledger (``DEBT``), the same
pattern as ``relayering_debt`` in ``tools/watchdog_consistency_audit.py``:

* a violation that is NOT in the ledger fails the suite (no new debt);
* a ledger entry that no longer occurs in the code fails the suite (paying debt off
  forces the ledger to shrink with it, so it can never again overstate the problem).

Imports inside functions, ``TYPE_CHECKING`` blocks, ``from common import x``,
``import common.x``, relative imports and literal ``importlib.import_module("...")`` calls
are all counted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Top-level import roots that common/ must never depend on (rules 2 and 3).
_FORBIDDEN_ROOTS = ("tools", "bootstrap")

# Leaf flat modules that have zero internal repo imports (guarded by
# test_leaf_flat_modules_have_no_repo_imports). Domain packages may import them without
# creating an import cycle.
_LEAF_FLAT_MODULES = frozenset({"common.console"})

# (importer path relative to the repo root, imported module).
#
# Debt owed to infra2#955. Every entry is a boundary violation that existed when the
# guard was introduced; none of them is endorsed. The ledger may only shrink: delete an
# entry in the change that removes the import (the test fails until you do), and never
# add one -- fix the import instead.
#
# History: 45 pairs when the guard landed, 42 after the common -> tools edges moved to
# common.deploy.refs, 2 after #955 moved the flat implementations into common.core,
# common.security, common.deploy and common.observability (the flat files were retired in
# #1059, see common/README.md).
#
# common.console is an explicit pure leaf module (_LEAF_FLAT_MODULES) with zero internal
# repo imports (guarded by test_leaf_flat_modules_have_no_repo_imports).
# Boundary debt is now zero.
_DEBT_ROWS: tuple[tuple[str, str], ...] = ()

DEBT: frozenset[tuple[str, str]] = frozenset(_DEBT_ROWS)


# --------------------------------------------------------------------------------------
# Extraction (pure functions of source text + the repo tree)
# --------------------------------------------------------------------------------------


def _libs_files(root: Path) -> list[Path]:
    """Every ``.py`` under ``common/`` except the test suite."""
    common = root / "common"
    return sorted(
        path
        for path in common.rglob("*.py")
        if "__pycache__" not in path.parts
        and "tests" not in path.relative_to(common).parts[:1]
    )


def _flat_modules(root: Path) -> set[str]:
    """``common.<name>`` for every top-level ``common/<name>.py`` (not ``__init__``)."""
    return {
        f"common.{path.stem}"
        for path in (root / "common").glob("*.py")
        if path.name != "__init__.py"
    }


def _domain_packages(root: Path) -> set[str]:
    """``common.<name>`` for every ``common/<name>/`` package other than the test suite."""
    return {
        f"common.{path.name}"
        for path in (root / "common").iterdir()
        if path.is_dir() and path.name != "tests" and (path / "__init__.py").exists()
    }


def _is_repo_module(dotted: str, root: Path) -> bool:
    """Whether ``dotted`` names a file or directory in the repo (namespace pkgs too).

    Case-exact on purpose: `Path.is_file()` is case-insensitive on macOS, so
    `common.core.Service` would "exist" as `common/core/service.py` there and not on Linux CI.
    """

    def entries(directory: Path) -> set[str]:
        return {p.name for p in directory.iterdir()} if directory.is_dir() else set()

    *parents, leaf = dotted.split(".")
    here = root
    for segment in parents:
        if segment not in entries(here) or not (here / segment).is_dir():
            return False
        here = here / segment
    names = entries(here)
    return (leaf in names and (here / leaf).is_dir()) or f"{leaf}.py" in names


def _absolute_base(node: ast.ImportFrom, package: str) -> str | None:
    """The absolute module a ``from ... import`` names, resolving relative levels."""
    if node.level == 0:
        return node.module
    parts = package.split(".")
    if node.level - 1 >= len(parts):
        return None  # climbs above the top-level package; not a resolvable edge
    anchor = parts[: len(parts) - (node.level - 1)]
    return ".".join([*anchor, node.module] if node.module else anchor)


def _imported_modules(source: str, package: str, root: Path) -> set[str]:
    """Every module a source text imports, as an absolute dotted name.

    ``package`` is the file's own package (``common.core`` for ``common/core/environ.py`` and
    for ``common/core/__init__.py``; ``common`` for ``common/common.py``), which is what relative
    imports resolve against. ``from X import y`` yields ``X.y`` when that names a module in
    the repo and ``X`` otherwise (``y`` is then a name, not a module).
    """
    flat = _flat_modules(root)
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_base(node, package)
            if base is None:
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if alias.name != "*" and _is_repo_module(candidate, root):
                    found.add(candidate)
                else:
                    found.add(base)
        elif isinstance(node, ast.Call):
            # importlib.import_module("x.y") / import_module("x.y") / __import__("x.y")
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if (
                name in {"import_module", "__import__"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and not node.args[0].value.startswith(".")
            ):
                found.add(node.args[0].value)

    # A flat module has no submodules, so `common.common.x` can only mean `common.common`.
    normalised: set[str] = set()
    for module in found:
        head = ".".join(module.split(".")[:2])
        normalised.add(head if head in flat else module)
    return normalised


def _violates(module: str, importer_in_domain: bool, root: Path) -> bool:
    segments = module.split(".")
    top = segments[0]
    if top in _FORBIDDEN_ROOTS:
        return True
    if top == "platform":
        # The stdlib `platform` is a plain module, never a package, so any
        # `platform.<x>` is the repo's `platform/` layer. `from platform import x`
        # is the stdlib unless `x` is a path in the repo (_imported_modules resolved it).
        return len(segments) > 1
    if importer_in_domain and top == "common":
        if module in _LEAF_FLAT_MODULES:
            return False
        return module == "common" or module in _flat_modules(root)
    return False


def collect_violations(root: Path = ROOT) -> set[tuple[str, str]]:
    """Every ``(importer path, imported module)`` that breaks a boundary rule."""
    domains = _domain_packages(root)
    violations: set[tuple[str, str]] = set()
    for path in _libs_files(root):
        relative = path.relative_to(root)
        package = ".".join(relative.parent.parts)
        in_domain = ".".join(relative.parts[:2]) in domains
        source = path.read_text(encoding="utf-8")
        for module in _imported_modules(source, package, root):
            if _violates(module, in_domain, root):
                violations.add((relative.as_posix(), module))
    return violations


# --------------------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------------------


def _render(pairs: set[tuple[str, str]]) -> str:
    return "\n".join(f"    ({path!r}, {module!r})," for path, module in sorted(pairs))


def test_no_import_boundary_violation_outside_the_debt_ledger() -> None:
    new = collect_violations() - DEBT
    assert not new, (
        "common/ has import-boundary violations that are not in DEBT (infra2#955). Fix the "
        "import instead of adding a ledger entry: a domain package imports "
        "common.<domain>.* (never a flat common/<name>.py), and nothing under common/ imports "
        "tools, platform or bootstrap.\n" + _render(new)
    )


def test_every_debt_entry_still_occurs_in_the_code() -> None:
    stale = DEBT - collect_violations()
    assert not stale, (
        "DEBT lists imports that no longer exist; the ledger only shrinks, so delete "
        "these rows in the change that paid them off (infra2#955):\n" + _render(stale)
    )


def test_the_debt_ledger_has_no_duplicate_rows() -> None:
    """A `frozenset` literal silently swallows a repeated row; the source tuple cannot."""
    assert len(_DEBT_ROWS) == len(DEBT), "duplicate rows in _DEBT_ROWS"


def test_the_scan_sees_the_real_tree() -> None:
    """GREEN-WHILE-EMPTY: a walk that finds no files or packages passes both rules."""
    files = {path.relative_to(ROOT).as_posix() for path in _libs_files(ROOT)}
    assert len(files) >= 50, files
    assert "common/deploy/preview.py" in files
    assert not any(name.startswith("common/tests/") for name in files)
    assert {
        "common.core",
        "common.security",
        "common.deploy",
        "common.observability",
        "common.backup",
    } <= _domain_packages(ROOT)
    assert {"common.common", "common.console"} <= _flat_modules(ROOT)


def test_a_libs_to_tools_import_is_no_longer_present_for_the_deploy_domain() -> None:
    """#955 item 3: the deploy backends take resolution from common, not from tools/."""
    owed = {pair for pair in DEBT if pair[0].startswith("common/deploy/")}
    assert not any(module.startswith("tools") for _path, module in owed), owed


# --------------------------------------------------------------------------------------
# The extractor itself (so the guard cannot go blind on a syntax it forgot)
# --------------------------------------------------------------------------------------


def _edges(source: str, package: str) -> set[str]:
    return _imported_modules(source, package, ROOT)


@pytest.mark.parametrize(
    ("source", "package", "expected"),
    [
        ("import common.common", "common.core", {"common.common"}),
        ("import common.common as c", "common.core", {"common.common"}),
        ("from common.common import infra_domain", "common.core", {"common.common"}),
        ("from common import common", "common.core", {"common.common"}),
        (
            "from common import common, console",
            "common.core",
            {"common.common", "common.console"},
        ),
        # relative, resolved against the importing file's package
        ("from .. import common", "common.core", {"common.common"}),
        ("from ..common import infra_domain", "common.core", {"common.common"}),
        ("from ... import common", "common.observability.watchers", {"common.common"}),
        ("from . import environ", "common.core", {"common.core.environ"}),
        ("from .environ import REPO_ROOT", "common.core", {"common.core.environ"}),
        # a flat module's `.` is `common`
        ("from . import common", "common", {"common.common"}),
        # inside a function and under TYPE_CHECKING
        (
            "def f():\n    from common.console import console\n",
            "common.security",
            {"common.console"},
        ),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import common.common\n",
            "common.core",
            {"common.common", "typing"},
        ),
        # dynamic imports with a literal target
        (
            "import importlib\nimportlib.import_module('common.common')",
            "common.core",
            {"common.common", "importlib"},
        ),
        ("__import__('tools.deploy_v2')", "common.core", {"tools.deploy_v2"}),
        # tools resolves to the module, whether named by `from tools import x` or not
        (
            "from tools import resolve_deploy_ref",
            "common.deploy",
            {"tools.resolve_deploy_ref"},
        ),
        (
            "from tools.resolve_deploy_ref import resolve_to_sha",
            "common.deploy",
            {"tools.resolve_deploy_ref"},
        ),
    ],
)
def test_extractor_resolves_every_import_form(
    source: str, package: str, expected: set[str]
) -> None:
    assert _edges(source, package) == expected


def test_extractor_does_not_treat_a_name_as_a_module() -> None:
    # `common.common` has no `infra_domain` submodule: it is a name, so the edge is common.common.
    assert _edges("from common.common import infra_domain", "common.core") == {
        "common.common"
    }
    assert _edges("from common.core import Service", "common.deploy") == {"common.core"}


@pytest.mark.parametrize(
    ("module", "in_domain", "expected"),
    [
        ("common.common", True, True),  # rule 1
        ("common.common", False, False),  # a flat module may import a flat module
        ("common.console", True, False),  # leaf flat module (_LEAF_FLAT_MODULES)
        ("common.core", True, False),  # domain -> domain is the intended direction
        ("common.core.environ", True, False),
        ("common", True, True),  # the flat root itself
        ("tools.deploy_v2", True, True),  # rule 2, from a domain package ...
        ("tools.deploy_v2", False, True),  # ... and from a flat module
        ("tools", False, True),
        ("bootstrap.x", False, True),  # rule 3
        ("platform.postgres", False, True),
        ("platform", False, False),  # stdlib `platform`
        ("os.path", True, False),
        ("infra2_sdk.refs", True, False),
    ],
)
def test_violation_rules(module: str, in_domain: bool, expected: bool) -> None:
    assert _violates(module, in_domain, ROOT) is expected


def test_leaf_flat_modules_have_no_repo_imports() -> None:
    """Every module in _LEAF_FLAT_MODULES must have zero internal repo imports."""
    internal_prefixes = (
        "common",
        "tools",
        "platform",
        "bootstrap",
        "finance_report",
        "truealpha",
    )
    for mod in sorted(_LEAF_FLAT_MODULES):
        stem = mod.removeprefix("common.")
        path = ROOT / "common" / f"{stem}.py"
        assert path.is_file(), f"Leaf flat module missing: {path}"
        imported = _imported_modules(path.read_text(encoding="utf-8"), "common", ROOT)
        bad = {imp for imp in imported if imp.split(".")[0] in internal_prefixes}
        assert not bad, (
            f"{mod} is declared in _LEAF_FLAT_MODULES and must have zero internal repo "
            f"imports, found: {sorted(bad)}"
        )


def test_collect_violations_on_a_synthetic_tree(tmp_path: Path) -> None:
    """End to end on a throwaway tree: each rule fires, and only where it applies."""
    common = tmp_path / "common"
    (common / "dom").mkdir(parents=True)
    (common / "tests").mkdir()
    (common / "__init__.py").write_text("")
    (common / "dom" / "__init__.py").write_text("")
    (common / "flat.py").write_text("")
    (common / "other.py").write_text("from common import flat\nfrom tools import x\n")
    (common / "dom" / "good.py").write_text("from common.dom import helper\n")
    (common / "dom" / "helper.py").write_text("")
    (common / "dom" / "bad.py").write_text(
        "from .. import flat\ndef f():\n    import bootstrap.thing\nimport platform\n"
    )
    # common/tests is exempt even though it imports everything
    (common / "tests" / "test_x.py").write_text("import common.flat\nimport tools.y\n")

    assert collect_violations(tmp_path) == {
        ("common/dom/bad.py", "common.flat"),
        ("common/dom/bad.py", "bootstrap.thing"),
        ("common/other.py", "tools"),
    }


RETIRED_FLAT_SHIMS_BATCH1 = {
    "availability_ledger",
    "backup_restore",
    "backup_verification",
    "container_breakdown",
    "container_breakdown_watch",
    "deploy_queue_guard",
    "infra_probes",
    "page_dedup",
    "probe_specs",
    "resident_watchers",
    "scheduler_peer_liveness",
    "watchdog_issue_trail",
    "watchdog_signal_entries",
}

RETIRED_FLAT_SHIMS_BATCH2 = {
    "coverage_regression",
    "deploy_queue",
    "harness_manifest",
    "harness_status",
    "harness_sweep",
    "release_markers",
    "service_identity",
}

RETIRED_FLAT_SHIMS_BATCH2C = {
    "secrets_registry",
    "secrets_supply",
    "vault_self_refresh_audit",
    "vault_tokens",
}

RETIRED_FLAT_SHIMS_DEPLOY = {
    "app_deploy_request",
    "deploy_contract",
    "deploy_env_config",
    "dokploy",
    "iac_runner_client",
}

RETIRED_FLAT_SHIMS_BATCH2D = {
    "env",
}

RETIRED_FLAT_SHIMS_BATCH3 = {
    "service_registry",
}

RETIRED_FLAT_SHIMS_FINAL = {
    "constants",
    "deployer",
    "deploy_dependencies",
    "environ",
    "facets",
    "registry",
    "service_facets",
}

ALL_RETIRED_FLAT_SHIMS = (
    RETIRED_FLAT_SHIMS_BATCH1
    | RETIRED_FLAT_SHIMS_BATCH2
    | RETIRED_FLAT_SHIMS_BATCH2C
    | RETIRED_FLAT_SHIMS_DEPLOY
    | RETIRED_FLAT_SHIMS_BATCH2D
    | RETIRED_FLAT_SHIMS_BATCH3
    | RETIRED_FLAT_SHIMS_FINAL
)

RETIRED_DOMAIN_SHIMS = (
    "common/core/ci_spec.py",
    "common/deploy/console.py",
    "common/deploy/git_provenance.py",
    "common/gate/coverage_regression.py",
    "common/observability/recency.py",
    "common/security/vault_tokens.py",
)


def test_retired_flat_shim_files_stay_deleted() -> None:
    """The retired flat shims must never be recreated."""
    resurrected = [
        name
        for name in sorted(ALL_RETIRED_FLAT_SHIMS)
        if (ROOT / "common" / f"{name}.py").exists()
    ]
    assert not resurrected, (
        f"Retired flat shim files recreated under common/: {resurrected}. Use the domain "
        "packages directly."
    )


def test_retired_domain_shims_and_libs_stay_deleted() -> None:
    """Retired domain shims and the legacy libs/ tree must stay deleted."""
    import importlib.util

    resurrected = [rel for rel in RETIRED_DOMAIN_SHIMS if (ROOT / rel).exists()]
    assert not resurrected, f"Retired domain shim files recreated: {resurrected}."
    libs_dir = ROOT / "libs"
    libs_py = (
        sorted(p.relative_to(ROOT).as_posix() for p in libs_dir.rglob("*.py"))
        if libs_dir.exists()
        else []
    )
    assert not libs_py, f"Legacy libs/ python files must not exist: {libs_py}"
    assert not libs_dir.exists(), (
        "Legacy libs/ directory must not exist on disk (residual __pycache__ "
        "makes 'libs' importable as a PEP-420 namespace package)."
    )
    assert importlib.util.find_spec("libs") is None, (
        "Legacy 'libs' package must not be importable."
    )


def test_no_tracked_python_file_uses_a_retired_flat_shim() -> None:
    """Every tracked python file must import domain packages rather than retired shims."""
    import subprocess

    repo = ROOT
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    violations: list[str] = []
    retired_dotted = {f"common.{s}" for s in ALL_RETIRED_FLAT_SHIMS}

    for rel in tracked:
        if rel == "common/tests/test_import_boundaries.py":
            continue
        path = repo / rel
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in retired_dotted:
                        violations.append(
                            f"{rel}:{node.lineno} imports retired {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "common":
                    for alias in node.names:
                        if alias.name in ALL_RETIRED_FLAT_SHIMS:
                            violations.append(
                                f"{rel}:{node.lineno} imports retired common.{alias.name}"
                            )
                elif mod in retired_dotted:
                    violations.append(f"{rel}:{node.lineno} imports from retired {mod}")

    assert not violations, (
        "Tracked python files still import retired flat shims:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_docs_do_not_cite_retired_flat_common_modules() -> None:
    """Package READMEs and SSOT docs must not cite retired flat common modules or libs/."""
    forbidden_tokens = (
        "(was `common/",
        "(was common/",
        "libs/",
        "common/coverage_regression.py",
        "common/app_deploy_request.py",
        "common/vault_self_refresh_audit",
        "common/harness_sweep.py",
        "common/alerting.py",
        "common/service_identity.py",
        "common/recency.py",
        "common.const",
        "common/observability/recency.py",
        "common/deploy/console.py",
        "common/security/vault_audit_ssh",
        "common/observability/alert_bridge",
        "common.observability.alert_bridge",
        "common/deploy/request.py",
        "common/harness/sweep.py",
    )
    doc_paths = sorted(
        [
            *(ROOT / "common").rglob("README.md"),
            ROOT / "tools" / "README.md",
            *(
                p
                for p in (ROOT / "docs" / "ssot").rglob("*")
                if p.is_file() and p.suffix in {".md", ".yaml", ".yml"}
            ),
        ]
    )
    violations: list[str] = []
    for path in doc_paths:
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for token in forbidden_tokens:
                if token in line:
                    violations.append(
                        f"{rel}:{lineno}: contains retired reference {token!r}"
                    )
    assert not violations, (
        "Documentation cites retired common/ shims or libs/ paths:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
