"""Import-boundary guard for ``libs/`` (infra2#955; #846 acceptance 1, #847).

#846 closed with the acceptance "domain packages do not import flat modules" and nothing
enforcing it: ``test_frozen_shims.py`` checks that a *shim* is pure, but never walks
``libs/<domain>/``, so the edges from domain packages back into flat modules (the #955
audit counted 56; two of the targets form import cycles with ``libs.core``) went unseen.
The layering ``libs/<domain>/`` -> ``libs/<flat>.py`` is the wrong way round: flat modules
are the compatibility surface, domain packages are where the implementation lives.

Rules, checked on the AST of every ``libs/**/*.py`` outside ``libs/tests`` (imports are
never executed, so a module that needs the SDK or a secret is still checked):

1. a file inside a ``libs`` domain package must not import a flat ``libs/<name>.py``;
2. nothing under ``libs/`` may import from ``tools`` (library code must not depend on
   the CLI layer that is built on top of it);
3. nothing under ``libs/`` may import from the ``platform`` or ``bootstrap`` Python
   packages (the deployment layers sit above ``libs``).

Existing violations are an explicit, **shrink-only** debt ledger (``DEBT``), the same
pattern as ``relayering_debt`` in ``tools/watchdog_consistency_audit.py``:

* a violation that is NOT in the ledger fails the suite (no new debt);
* a ledger entry that no longer occurs in the code fails the suite (paying debt off
  forces the ledger to shrink with it, so it can never again overstate the problem).

Imports inside functions, ``TYPE_CHECKING`` blocks, ``from libs import x``,
``import libs.x``, relative imports and literal ``importlib.import_module("...")`` calls
are all counted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Top-level import roots that libs/ must never depend on (rules 2 and 3).
_FORBIDDEN_ROOTS = ("tools", "bootstrap")

# (importer path relative to the repo root, imported module).
#
# Debt owed to infra2#955. Every entry is a boundary violation that existed when the
# guard was introduced; none of them is endorsed. The ledger may only shrink: delete an
# entry in the change that removes the import (the test fails until you do), and never
# add one -- fix the import instead.
#
# 1. domain package -> flat module. 13 of these 42 pairs (27 of the 89 import
#    statements behind them) point at the three modules that still hold their
#    implementation: libs.common, libs.env, libs.service_registry. Moving those into
#    domain packages (and breaking the common <-> core.environ and
#    service_registry <-> core.service cycles) retires most of this list.
# 2. libs -> tools (library depending on the CLI layer): none remain. The
#    libs/deploy -> tools.resolve_deploy_ref / tools.openpanel_clients edges were retired
#    by moving the resolver to libs.deploy.refs; the ledger started at 45 pairs and
#    this change took it to 42.
_DEBT_ROWS: tuple[tuple[str, str], ...] = (
    ("libs/backup/verification.py", "libs.service_registry"),
    ("libs/core/environ.py", "libs.common"),
    ("libs/core/service.py", "libs.service_registry"),
    ("libs/deploy/deployer.py", "libs.common"),
    ("libs/deploy/deployer.py", "libs.console"),
    ("libs/deploy/deployer.py", "libs.deploy_dependencies"),
    ("libs/deploy/deployer.py", "libs.deploy_queue"),
    ("libs/deploy/deployer.py", "libs.dokploy"),
    ("libs/deploy/deployer.py", "libs.env"),
    ("libs/deploy/deployer.py", "libs.secrets_registry"),
    ("libs/deploy/deployer.py", "libs.service_facets"),
    ("libs/deploy/deployer.py", "libs.service_registry"),
    ("libs/deploy/preview.py", "libs.common"),
    ("libs/deploy/preview.py", "libs.compose_lock"),
    ("libs/deploy/preview.py", "libs.deploy_contract"),
    ("libs/deploy/preview.py", "libs.deploy_env_config"),
    ("libs/deploy/promote.py", "libs.common"),
    ("libs/deploy/promote.py", "libs.compose_lock"),
    ("libs/deploy/promote.py", "libs.console"),
    ("libs/deploy/promote.py", "libs.deploy_contract"),
    ("libs/deploy/promote.py", "libs.deploy_env_config"),
    ("libs/deploy/promote.py", "libs.deploy_queue"),
    ("libs/deploy/promote.py", "libs.env"),
    ("libs/deploy/promote.py", "libs.service_registry"),
    ("libs/deploy/schema_gate.py", "libs.service_registry"),
    ("libs/observability/breakdown.py", "libs.deploy_env_config"),
    ("libs/observability/breakdown.py", "libs.service_registry"),
    ("libs/observability/issue_trail.py", "libs.scheduler_peer_liveness"),
    ("libs/observability/probes.py", "libs.probe_specs"),
    ("libs/observability/watchers/breakdown_watch.py", "libs.recency"),
    ("libs/observability/watchers/breakdown_watch.py", "libs.resident_watchers"),
    ("libs/security/prune.py", "libs.secrets_registry"),
    ("libs/security/store.py", "libs.env"),
    ("libs/security/supply.py", "libs.secrets_registry"),
)

DEBT: frozenset[tuple[str, str]] = frozenset(_DEBT_ROWS)


# --------------------------------------------------------------------------------------
# Extraction (pure functions of source text + the repo tree)
# --------------------------------------------------------------------------------------


def _libs_files(root: Path) -> list[Path]:
    """Every ``.py`` under ``libs/`` except the test suite."""
    libs = root / "libs"
    return sorted(
        path
        for path in libs.rglob("*.py")
        if "__pycache__" not in path.parts
        and "tests" not in path.relative_to(libs).parts[:1]
    )


def _flat_modules(root: Path) -> set[str]:
    """``libs.<name>`` for every top-level ``libs/<name>.py`` (not ``__init__``)."""
    return {
        f"libs.{path.stem}"
        for path in (root / "libs").glob("*.py")
        if path.name != "__init__.py"
    }


def _domain_packages(root: Path) -> set[str]:
    """``libs.<name>`` for every ``libs/<name>/`` package other than the test suite."""
    return {
        f"libs.{path.name}"
        for path in (root / "libs").iterdir()
        if path.is_dir() and path.name != "tests" and (path / "__init__.py").exists()
    }


def _is_repo_module(dotted: str, root: Path) -> bool:
    """Whether ``dotted`` names a file or directory in the repo (namespace pkgs too).

    Case-exact on purpose: `Path.is_file()` is case-insensitive on macOS, so
    `libs.core.Service` would "exist" as `libs/core/service.py` there and not on Linux CI.
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

    ``package`` is the file's own package (``libs.core`` for ``libs/core/environ.py`` and
    for ``libs/core/__init__.py``; ``libs`` for ``libs/common.py``), which is what relative
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

    # A flat module has no submodules, so `libs.common.x` can only mean `libs.common`.
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
    if importer_in_domain and top == "libs":
        return module == "libs" or module in _flat_modules(root)
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
        "libs/ has import-boundary violations that are not in DEBT (infra2#955). Fix the "
        "import instead of adding a ledger entry: a domain package imports "
        "libs.<domain>.* (never a flat libs/<name>.py), and nothing under libs/ imports "
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
    assert "libs/deploy/preview.py" in files
    assert not any(name.startswith("libs/tests/") for name in files)
    assert {
        "libs.core",
        "libs.security",
        "libs.deploy",
        "libs.observability",
        "libs.backup",
    } <= _domain_packages(ROOT)
    assert {"libs.common", "libs.env", "libs.service_registry"} <= _flat_modules(ROOT)


def test_a_libs_to_tools_import_is_no_longer_present_for_the_deploy_domain() -> None:
    """#955 item 3: the deploy backends take resolution from libs, not from tools/."""
    owed = {pair for pair in DEBT if pair[0].startswith("libs/deploy/")}
    assert not any(module.startswith("tools") for _path, module in owed), owed


# --------------------------------------------------------------------------------------
# The extractor itself (so the guard cannot go blind on a syntax it forgot)
# --------------------------------------------------------------------------------------


def _edges(source: str, package: str) -> set[str]:
    return _imported_modules(source, package, ROOT)


@pytest.mark.parametrize(
    ("source", "package", "expected"),
    [
        ("import libs.common", "libs.core", {"libs.common"}),
        ("import libs.common as c", "libs.core", {"libs.common"}),
        ("from libs.common import infra_domain", "libs.core", {"libs.common"}),
        ("from libs import common", "libs.core", {"libs.common"}),
        ("from libs import common, env", "libs.core", {"libs.common", "libs.env"}),
        # relative, resolved against the importing file's package
        ("from .. import common", "libs.core", {"libs.common"}),
        ("from ..common import infra_domain", "libs.core", {"libs.common"}),
        ("from ... import common", "libs.observability.watchers", {"libs.common"}),
        ("from . import environ", "libs.core", {"libs.core.environ"}),
        ("from .environ import REPO_ROOT", "libs.core", {"libs.core.environ"}),
        # a flat module's `.` is `libs`
        ("from . import common", "libs", {"libs.common"}),
        # inside a function and under TYPE_CHECKING
        (
            "def f():\n    from libs.env import VaultSecrets\n",
            "libs.security",
            {"libs.env"},
        ),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import libs.common\n",
            "libs.core",
            {"libs.common", "typing"},
        ),
        # dynamic imports with a literal target
        (
            "import importlib\nimportlib.import_module('libs.common')",
            "libs.core",
            {"libs.common", "importlib"},
        ),
        ("__import__('tools.deploy_v2')", "libs.core", {"tools.deploy_v2"}),
        # tools resolves to the module, whether named by `from tools import x` or not
        (
            "from tools import resolve_deploy_ref",
            "libs.deploy",
            {"tools.resolve_deploy_ref"},
        ),
        (
            "from tools.resolve_deploy_ref import resolve_to_sha",
            "libs.deploy",
            {"tools.resolve_deploy_ref"},
        ),
    ],
)
def test_extractor_resolves_every_import_form(
    source: str, package: str, expected: set[str]
) -> None:
    assert _edges(source, package) == expected


def test_extractor_does_not_treat_a_name_as_a_module() -> None:
    # `libs.common` has no `infra_domain` submodule: it is a name, so the edge is libs.common.
    assert _edges("from libs.common import infra_domain", "libs.core") == {
        "libs.common"
    }
    assert _edges("from libs.core import Service", "libs.deploy") == {"libs.core"}


@pytest.mark.parametrize(
    ("module", "in_domain", "expected"),
    [
        ("libs.common", True, True),  # rule 1
        ("libs.common", False, False),  # a flat module may import a flat module
        ("libs.core", True, False),  # domain -> domain is the intended direction
        ("libs.core.environ", True, False),
        ("libs", True, True),  # the flat root itself
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


def test_collect_violations_on_a_synthetic_tree(tmp_path: Path) -> None:
    """End to end on a throwaway tree: each rule fires, and only where it applies."""
    libs = tmp_path / "libs"
    (libs / "dom").mkdir(parents=True)
    (libs / "tests").mkdir()
    (libs / "__init__.py").write_text("")
    (libs / "dom" / "__init__.py").write_text("")
    (libs / "flat.py").write_text("")
    (libs / "other.py").write_text("from libs import flat\nfrom tools import x\n")
    (libs / "dom" / "good.py").write_text("from libs.dom import helper\n")
    (libs / "dom" / "helper.py").write_text("")
    (libs / "dom" / "bad.py").write_text(
        "from .. import flat\ndef f():\n    import bootstrap.thing\nimport platform\n"
    )
    # libs/tests is exempt even though it imports everything
    (libs / "tests" / "test_x.py").write_text("import libs.flat\nimport tools.y\n")

    assert collect_violations(tmp_path) == {
        ("libs/dom/bad.py", "libs.flat"),
        ("libs/dom/bad.py", "bootstrap.thing"),
        ("libs/other.py", "tools"),
    }
