"""Import-boundary guard for infra2 (infra2#955, #846, #847; #1823 Step 4.3).

docs/ssot/core.harness.md §5.3 defines the layers; the ranks below implement it with the SDK
checker ``infra2_sdk.rules.imports.check_import_boundaries`` (version: uv.lock) over the whole
repository. A module may import from its own layer and from lower ranks only:

| Rank | Layer | Modules |
|---|---|---|
| 0 | ``common`` | domain packages ``common.<domain>`` and the leaf module ``common.console`` |
| 1 | ``flat`` | the other flat modules ``common/<name>.py`` |
| 2 | ``tools`` | ``tools.*`` |
| 3 | ``deploy`` | ``platform.*`` and ``bootstrap.*`` |
| 4 | ``tests`` | ``common.tests.*`` |

So a domain package does not import a flat module, nothing under ``common/`` imports
``tools``, ``platform`` or ``bootstrap``, and ``tools`` does not import the deployment layers.
Imports inside functions, ``TYPE_CHECKING`` blocks, relative imports and literal
``importlib.import_module("...")`` calls are all counted. Hidden directories (``.venv``)
are not scanned.
"""

from __future__ import annotations

import ast
from pathlib import Path

from infra2_sdk.rules.imports import (
    LayerRule,
    check_import_boundaries,
    scan_module_imports,
)

ROOT = Path(__file__).resolve().parents[2]

# Leaf flat modules with zero internal repo imports. Domain packages may import them
# (test_leaf_flat_modules_have_no_repo_imports keeps them leaves).
_LEAF_FLAT_MODULES = frozenset({"common.console"})


def _flat_modules(root: Path) -> tuple[str, ...]:
    """``common.<name>`` for each flat ``common/<name>.py`` that is not a leaf."""
    return tuple(
        sorted(
            f"common.{path.stem}"
            for path in (root / "common").glob("*.py")
            if path.name != "__init__.py"
            and f"common.{path.stem}" not in _LEAF_FLAT_MODULES
        )
    )


def layers(root: Path = ROOT) -> list[LayerRule]:
    return [
        LayerRule("common", ("common",), 0),
        LayerRule("flat", _flat_modules(root), 1),
        LayerRule("tools", ("tools",), 2),
        LayerRule("deploy", ("platform", "bootstrap"), 3),
        LayerRule("tests", ("common.tests",), 4),
    ]


def collect_violations(root: Path = ROOT) -> set[tuple[str, str]]:
    """Every ``(importer module, imported module)`` that breaks a layer rule."""
    return {
        (v.source_module, v.target_module)
        for v in check_import_boundaries(
            root, layers(root), forbid_cycles=False, ignore_type_checking=False
        )
    }


def test_no_import_boundary_violation() -> None:
    violations = collect_violations()
    assert violations == set(), (
        "import-boundary violations (docs/ssot/core.harness.md §5.3); fix the import:\n"
        + "\n".join(f"    {src} -> {dst}" for src, dst in sorted(violations))
    )


def test_the_scan_sees_the_real_tree() -> None:
    """GREEN-WHILE-EMPTY: a scan that finds no modules passes every rule."""
    graph = scan_module_imports(ROOT, ignore_type_checking=False)
    assert "common.deploy.preview" in graph
    assert "tools.pr_merge_gate" in graph
    assert not any(module.startswith((".venv", "venv")) for module in graph)
    assert sum(module.startswith("common.") for module in graph) >= 100
    assert {"common.common"} <= set(_flat_modules(ROOT))


def test_leaf_flat_modules_have_no_repo_imports() -> None:
    graph = scan_module_imports(ROOT, ignore_type_checking=False)
    for module in sorted(_LEAF_FLAT_MODULES):
        assert module in graph, f"leaf flat module missing: {module}"
        assert graph[module] == set(), (
            f"{module} must import no repo module: {graph[module]}"
        )


def test_collect_violations_on_a_synthetic_tree(tmp_path: Path) -> None:
    """End to end on a throwaway tree: each rule fires, and only where it applies."""
    common = tmp_path / "common"
    (common / "dom").mkdir(parents=True)
    (common / "tests").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "bootstrap").mkdir()
    (common / "__init__.py").write_text("")
    (common / "dom" / "__init__.py").write_text("")
    (common / "console.py").write_text("")
    (common / "flat.py").write_text("")
    (common / "other.py").write_text("from common import flat\nfrom tools import x\n")
    (common / "dom" / "good.py").write_text(
        "from common.dom import helper\nimport common.console\n"
    )
    (common / "dom" / "helper.py").write_text("")
    (common / "dom" / "bad.py").write_text(
        "from .. import flat\ndef f():\n    import bootstrap.thing\nimport platform\n"
    )
    (common / "dom" / "late.py").write_text(
        "import importlib\nimportlib.import_module('tools.x')\n"
    )
    (tmp_path / "tools" / "x.py").write_text("import bootstrap.thing\n")
    (tmp_path / "bootstrap" / "thing.py").write_text("")
    (common / "dom" / "typed.py").write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import tools.x\n"
    )
    # common/tests is the top layer: it may import everything
    (common / "tests" / "test_x.py").write_text("import common.flat\nimport tools.x\n")

    assert collect_violations(tmp_path) == {
        ("common.dom.bad", "common.flat"),
        ("common.dom.bad", "bootstrap.thing"),
        ("common.other", "tools.x"),
        ("common.dom.late", "tools.x"),
        ("common.dom.typed", "tools.x"),
        ("tools.x", "bootstrap.thing"),
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
    "estate_scheduler_liveness",
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
    "common/deploy/app_deploy_request.py",
    "common/deploy/app_deploy_request_cli.py",
    "common/deploy/console.py",
    "common/deploy/git_provenance.py",
    "common/deploy/refs.py",
    "common/gate/coverage_regression.py",
    "common/observability/recency.py",
    "common/security/vault_tokens.py",
    "tools/app_deploy_request.py",
    "tools/resolve_deploy_ref.py",
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
        "common/deploy/app_deploy_request.py",
        "common/deploy/refs.py",
        "tools/app_deploy_request.py",
        "tools/resolve_deploy_ref.py",
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


def _upper_layer_imports(root: Path) -> set[tuple[str, str]]:
    """Import statements in ``common/`` (not tests) whose top name is an upper layer.

    The SDK check sees an edge only when its target is a file under the root. This catches
    the rest: ``import tools``, an import of a module that does not exist (a ``try``/``except
    ImportError`` fallback), and ``__import__("tools.x")``.
    """
    upper = {"tools", "platform", "bootstrap"}
    found: set[tuple[str, str]] = set()
    for path in sorted((root / "common").rglob("*.py")):
        rel = path.relative_to(root)
        if "tests" in rel.parts or "__pycache__" in rel.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names = [node.args[0].value]
            for name in names:
                if name.split(".")[0] in upper and name != "platform":
                    found.add((rel.as_posix(), name))
    return found


def test_common_names_no_upper_layer_even_without_a_target_file() -> None:
    assert _upper_layer_imports(ROOT) == set()


def test_upper_layer_imports_on_a_synthetic_tree(tmp_path: Path) -> None:
    common = tmp_path / "common"
    (common / "tests").mkdir(parents=True)
    (common / "a.py").write_text("import tools\nimport platform\n")
    (common / "b.py").write_text(
        "try:\n    from tools.gone import x\nexcept ImportError:\n    x = None\n"
    )
    (common / "c.py").write_text('m = __import__("bootstrap.thing")\n')
    (common / "tests" / "test_x.py").write_text("import tools\n")
    assert _upper_layer_imports(tmp_path) == {
        ("common/a.py", "tools"),
        ("common/b.py", "tools.gone"),
        ("common/c.py", "bootstrap.thing"),
    }
