#!/usr/bin/env python3
"""Measure infra2-sdk public symbol and environment-key usage across consumer repositories.

This CLI tool promotes the census scripts into one tested command that computes
six metrics (M1 through M6):
  M1: Public symbol reachability across consumers (prod_used, test_only, internal_only, unused)
      and missing consumer imports.
  M2: runtime_env_contract() key reachability across consumers (prod_used, test_only, unused)
      and C1 through C6 classifications.
  M3: Consumer infra2-sdk version pin lag relative to the latest SDK release version.
  M4: Consumer references to symbols listed in LEFT_THE_PACKAGES, residual libs/ or tools/_lib/
      directories, and except ImportError SDK fallback copies (must equal 0).
  M5: Consumer hardcoded SERVICE_NAME or OTEL key lists outside sdk_vocabulary() (must equal 0).
  M6: Consumer tools/*.py cross-imports (from tools... or import tools... inside tools/, must equal 0).
"""

from __future__ import annotations

import argparse
import ast
import csv
import importlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.core import env_inventory as envinv  # noqa: E402

CENSUS_SELF_REL_PATHS = frozenset(
    {
        "tools/sdk_usage_census.py",
        "common/core/sdk_usage_census.py",
    }
)


def sdk_requirement(path: Path = REPO_ROOT / "uv.lock") -> str:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/sdk_requirement.py"), str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


SKIP_SYMBOL_NAMES = {"main", "build_parser", "__version__", "__all__"}
MODULE_DUNDER_ATTRS = frozenset(
    {
        "__all__",
        "__cached__",
        "__doc__",
        "__file__",
        "__loader__",
        "__name__",
        "__package__",
        "__path__",
        "__spec__",
        "__version__",
    }
)

SKIP_DIR_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    "dist",
    "build",
    ".agents",
}

DEFAULT_LEFT_THE_PACKAGES: tuple[str, ...] = (
    "ENV_FALLBACK_KEYS",
    "CanonicalEnv",
    "EnvVarSpec",
    "RUNTIME_ENV_SPECS",
    "resolve_runtime_env_value",
    "is_canonical_env",
    "normalize_env",
    "parse_env",
)

SDK_EXTRA_KEYS: dict[str, tuple[str, str]] = {
    "GITHUB_ACTIONS": ("helper", "runtime/environment.py"),
    "VAULT_ADDR": ("helper", "secrets.py"),
    "VAULT_TOKEN": ("helper", "secrets.py"),
    "VAULT_ROLE_ID": ("helper", "secrets.py"),
    "VAULT_SECRET_ID": ("helper", "secrets.py"),
    "GITHUB_TOKEN": ("tool-token", "deploy.py"),
    "INFRA2_PAT": ("tool-token", "deploy.py"),
    "ENV_SUFFIX": ("template", "scaffold.py"),
}

OTEL_COMPANION_KEYS = {"SERVICE_VERSION", "ENVIRONMENT", "ENV", "APP_ENV"}
SELF_REPO = "infra2-sdk"


def is_test_path(path: str) -> bool:
    """Return True when the relative path belongs to a test or fixture file."""
    return bool(
        re.search(
            r"(^|/)(tests?|__tests__|e2e|e2e_regressions|fixtures)/"
            r"|(^|/)test_[^/]*\.py$"
            r"|(^|/)conftest\.py$"
            r"|_test\.py$",
            path,
        )
    )


def resolve_sdk_pkg_root(sdk_src: Path | None = None) -> Path:
    """Locate the infra2_sdk package directory from an explicit path, submodule, or site-packages."""
    if sdk_src is not None:
        candidate = sdk_src.resolve()
        if (candidate / "__init__.py").is_file():
            return candidate
        if (candidate / "infra2_sdk" / "__init__.py").is_file():
            return candidate / "infra2_sdk"
        if (candidate / "src" / "infra2_sdk" / "__init__.py").is_file():
            return candidate / "src" / "infra2_sdk"
        raise FileNotFoundError(f"infra2_sdk package not found under {sdk_src}")

    sub_pkg = REPO_ROOT / "repos" / "infra2-sdk" / "src" / "infra2_sdk"
    if (sub_pkg / "__init__.py").is_file():
        return sub_pkg

    import infra2_sdk

    return Path(infra2_sdk.__file__).resolve().parent


def default_consumers(root: Path = REPO_ROOT) -> dict[str, Path]:
    """Return available consumer repository roots."""
    consumers: dict[str, Path] = {"infra2": root}
    for name in ("finance_report", "truealpha"):
        sub = root / "repos" / name
        if (
            (sub / ".git").exists()
            or (sub / "pyproject.toml").is_file()
            or (sub / "apps" / "backend" / "pyproject.toml").is_file()
        ):
            consumers[name] = sub
    return consumers


def list_repo_files(root: Path, suffix: str | None = None) -> list[str]:
    """List tracked or untracked non-ignored files in a repository root."""
    if (root / ".git").exists():
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        raw = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    else:
        raw = [
            str(p.relative_to(root)).replace("\\", "/")
            for p in sorted(root.rglob("*"))
            if p.is_file()
        ]
    out: list[str] = []
    for rel in raw:
        if rel.startswith("repos/"):
            continue
        parts = rel.split("/")
        if any(part in SKIP_DIR_PARTS for part in parts):
            continue
        if suffix and not rel.endswith(suffix):
            continue
        if (root / rel).is_file():
            out.append(rel)
    return sorted(set(out))


def module_name(path: Path, pkg_root: Path) -> str:
    """Convert a file path inside infra2_sdk into its dotted module name."""
    rel = path.relative_to(pkg_root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_overload_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when a function definition is decorated with @overload or @typing.overload."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "overload":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "overload":
            return True
    return False


def public_defs(pkg_root: Path) -> list[dict[str, Any]]:
    """Collect public module-level functions, classes, and constants in infra2_sdk."""
    defs: list[dict[str, Any]] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel_parts = path.relative_to(pkg_root).with_suffix("").parts
        if any(p in SKIP_DIR_PARTS for p in rel_parts):
            continue
        if any(p.startswith("_") and p != "__init__" for p in rel_parts):
            continue
        if path.name == "__init__.py":
            continue
        mod = module_name(path, pkg_root)
        try:
            tree = ast.parse(path.read_text("utf-8", "replace"))
        except SyntaxError:
            continue
        exported: set[str] | None = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "__all__" for t in node.targets
            ):
                if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                    elts = {
                        elt.value
                        for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    }
                    exported = elts if elts else None
        for node in tree.body:
            kind: str | None = None
            name: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_overload_stub(node):
                    continue
                kind, name = "function", node.name
            elif isinstance(node, ast.ClassDef):
                kind, name = "class", node.name
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.isupper()
            ):
                kind, name = "constant", node.targets[0].id
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id.isupper()
            ):
                kind, name = "constant", node.target.id
            if not name or name.startswith("_") or name in SKIP_SYMBOL_NAMES:
                continue
            if exported is not None and name not in exported:
                continue
            defs.append(
                {
                    "module": mod,
                    "name": name,
                    "kind": kind,
                    "line": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno),
                    "path": str(path),
                }
            )
    return defs


def sdk_internal_refs(pkg_root: Path) -> dict[str, list[tuple[str, int]]]:
    """Map symbol name to [(file, line)] of internal loads in the SDK (excluding __init__ re-exports)."""
    refs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in sorted(pkg_root.rglob("*.py")):
        reexport = path.name == "__init__.py"
        try:
            tree = ast.parse(path.read_text("utf-8", "replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and not reexport:
                for alias in node.names:
                    refs[alias.name].append((str(path), node.lineno))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                refs[node.id].append((str(path), node.lineno))
            elif isinstance(node, ast.Attribute):
                refs[node.attr].append((str(path), node.lineno))
    return refs


def sdk_module_exports(pkg_root: Path) -> dict[str, set[str]]:
    """Build a map of dotted module or class path to defined/exported attribute names from AST."""
    exports: dict[str, set[str]] = defaultdict(set)
    star_reexports: list[tuple[str, str]] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel_parts = path.relative_to(pkg_root).with_suffix("").parts
        if any(p in SKIP_DIR_PARTS for p in rel_parts):
            continue
        mod = module_name(path, pkg_root)
        exports.setdefault(mod, set())
        parts = mod.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            exports[parent].add(parts[i])
        try:
            tree = ast.parse(path.read_text("utf-8", "replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                exports[mod].add(node.name)
            elif isinstance(node, ast.ClassDef):
                exports[mod].add(node.name)
                cls_key = f"{mod}.{node.name}"
                for stmt in ast.walk(node):
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        exports[cls_key].add(stmt.name)
                    elif isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name):
                                exports[cls_key].add(target.id)
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(
                        stmt.target, ast.Name
                    ):
                        exports[cls_key].add(stmt.target.id)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        exports[mod].add(target.id)
                        if target.id == "__all__" and isinstance(
                            node.value, (ast.List, ast.Tuple, ast.Set)
                        ):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(
                                    elt.value, str
                                ):
                                    exports[mod].add(elt.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                exports[mod].add(node.target.id)
            elif isinstance(node, ast.ImportFrom):
                src_mod = node.module or ""
                if node.level and path.name == "__init__.py":
                    base_parts = mod.split(".")
                elif node.level:
                    base_parts = mod.split(".")[:-1]
                else:
                    base_parts = []
                if node.level > 1:
                    base_parts = base_parts[: -(node.level - 1)]
                resolved_src = (
                    ".".join([*base_parts, src_mod])
                    if base_parts and src_mod
                    else (".".join(base_parts) if base_parts else src_mod)
                )
                for alias in node.names:
                    if alias.name == "*":
                        if resolved_src:
                            star_reexports.append((mod, resolved_src))
                    else:
                        exports[mod].add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    exports[mod].add(alias.asname or alias.name.split(".")[0])
    changed = True
    while changed:
        changed = False
        for dst_mod, src_mod in star_reexports:
            if src_mod in exports:
                before = len(exports[dst_mod])
                exports[dst_mod] |= exports[src_mod]
                if len(exports[dst_mod]) != before:
                    changed = True
    return exports


def _consumer_repo_tokens(
    trees: list[tuple[str, bool, ast.AST]],
) -> tuple[set[str], set[str]]:
    """Collect loaded identifier and attribute names across prod and test ASTs in one consumer repo."""
    prod_tokens: set[str] = set()
    test_tokens: set[str] = set()
    for _rel, is_test, tree in trees:
        target = test_tokens if is_test else prod_tokens
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                target.add(node.id)
            elif isinstance(node, ast.Attribute):
                target.add(node.attr)
    return prod_tokens, test_tokens


def _enclosing_method_is_dead_or_test_only(
    tree: ast.AST,
    sdk_local_names: set[str],
    prod_tokens: set[str],
    test_tokens: set[str],
) -> tuple[set[str], set[str]]:
    """Return (downgraded_to_test_only, downgraded_to_dead) for SDK names referenced only inside uncalled methods or functions."""
    active_by_local: dict[str, list[str | None]] = defaultdict(list)

    def _visit_block(stmts: list[ast.stmt], enclosing_method: str | None) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                for item in stmt.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        mname = None if item.name.startswith("__") else item.name
                        _visit_block(item.body, mname)
                    else:
                        _visit_block([item], None)
                continue
            if (
                isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                and enclosing_method is None
            ):
                fn_ctx = (
                    None
                    if (
                        stmt.name == "main"
                        or stmt.name.startswith("__")
                        or bool(stmt.decorator_list)
                    )
                    else stmt.name
                )
                _visit_block(stmt.body, fn_ctx)
                continue
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    continue
                if (
                    isinstance(sub, ast.Name)
                    and isinstance(sub.ctx, ast.Load)
                    and sub.id in sdk_local_names
                ):
                    active_by_local[sub.id].append(enclosing_method)

    if isinstance(tree, ast.Module):
        _visit_block(tree.body, None)

    to_test: set[str] = set()
    to_dead: set[str] = set()
    for local_name in sdk_local_names:
        contexts = active_by_local.get(local_name)
        if not contexts:
            continue
        if any(ctx is None or ctx in prod_tokens for ctx in contexts):
            continue
        if any(ctx in test_tokens for ctx in contexts if ctx is not None):
            to_test.add(local_name)
        else:
            to_dead.add(local_name)
    return to_test, to_dead


def consumer_symbol_refs(
    consumers: dict[str, Path],
    left_the_packages: tuple[str, ...] = DEFAULT_LEFT_THE_PACKAGES,
    package_scoped_left: dict[str, set[str]] | None = None,
) -> tuple[
    dict[str, Counter[str]],
    dict[str, Counter[str]],
    dict[tuple[str, str], list[str]],
]:
    """Scan consumer Python files for infra2_sdk imports and module attribute accesses."""
    prod: dict[str, Counter[str]] = defaultdict(Counter)
    test: dict[str, Counter[str]] = defaultdict(Counter)
    seen_imports: dict[tuple[str, str], list[str]] = defaultdict(list)
    retired_set = set(left_the_packages)
    pkg_left = package_scoped_left or {}

    for repo, root in sorted(consumers.items()):
        parsed_files: list[tuple[str, bool, ast.AST]] = []
        for rel in list_repo_files(root, suffix=".py"):
            try:
                source = (root / rel).read_text("utf-8", "replace")
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            parsed_files.append((rel, is_test_path(rel), tree))

        prod_tokens, test_tokens = _consumer_repo_tokens(parsed_files)

        for rel, is_test, tree in parsed_files:
            bucket = test if is_test else prod
            mod_alias: dict[str, str] = {}
            local_to_sdk: dict[str, str] = {}
            star_modules: list[str] = []
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] == "infra2_sdk"
                ):
                    for alias in node.names:
                        if alias.name == "*":
                            star_modules.append(node.module)
                            continue
                        if alias.name in MODULE_DUNDER_ATTRS:
                            continue
                        local_bound = alias.asname or alias.name
                        local_to_sdk[local_bound] = alias.name
                        seen_imports[(node.module, alias.name)].append(
                            f"{repo}:{rel}:{node.lineno}"
                        )
                        mod_alias[local_bound] = f"{node.module}.{alias.name}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "infra2_sdk":
                            bound = alias.asname or alias.name.split(".")[0]
                            mod_alias[bound] = alias.name

            if not is_test and local_to_sdk:
                to_test_locals, to_dead_locals = _enclosing_method_is_dead_or_test_only(
                    tree, set(local_to_sdk), prod_tokens, test_tokens
                )
            else:
                to_test_locals, to_dead_locals = set(), set()

            for local_bound, sdk_name in local_to_sdk.items():
                if local_bound in to_dead_locals:
                    continue
                if local_bound in to_test_locals:
                    test[sdk_name][repo] += 1
                else:
                    bucket[sdk_name][repo] += 1

            star_retired_for_file = set(retired_set)
            for smod in star_modules:
                star_retired_for_file |= pkg_left.get(smod, set())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in mod_alias
                ):
                    if node.attr in MODULE_DUNDER_ATTRS:
                        continue
                    target_base = mod_alias[node.value.id]
                    if node.value.id in to_dead_locals:
                        pass
                    elif node.value.id in to_test_locals:
                        test[node.attr][repo] += 1
                    else:
                        bucket[node.attr][repo] += 1
                    seen_imports[(target_base, node.attr)].append(
                        f"{repo}:{rel}:{node.lineno}"
                    )
                elif (
                    star_modules
                    and isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in star_retired_for_file
                ):
                    bucket[node.id][repo] += 1
                    seen_imports[(star_modules[0], node.id)].append(
                        f"{repo}:{rel}:{node.lineno}"
                    )
    return prod, test, seen_imports


def cli_uses(consumers: dict[str, Path]) -> dict[str, int]:
    """Count `-m infra2_sdk.<module>` CLI invocations across consumer files."""
    uses: Counter[str] = Counter()
    pattern = re.compile(r"-m\s+(infra2_sdk(?:\.[_a-z0-9]+)*)")
    exts = (".yml", ".yaml", ".sh", ".toml", ".py", ".md")
    for repo, root in sorted(consumers.items()):
        for rel in list_repo_files(root):
            if not (rel.endswith(exts) or rel.endswith("Makefile")):
                continue
            try:
                text = (root / rel).read_text("utf-8", "replace")
            except OSError:
                continue
            for match in pattern.finditer(text):
                uses[f"{repo}:{match.group(1)}"] += 1
    return dict(sorted(uses.items()))


def check_consumer_imports(
    seen_imports: dict[tuple[str, str], list[str]],
    pkg_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify that every (module, name) imported by consumers exists in pkg_root."""
    exports = sdk_module_exports(pkg_root)
    installed_pkg: Path | None = None
    try:
        import infra2_sdk

        installed_pkg = Path(infra2_sdk.__file__).resolve().parent
    except Exception:  # noqa: BLE001
        installed_pkg = None

    use_importlib = (
        installed_pkg is not None and pkg_root.resolve() == installed_pkg.resolve()
    )

    def exists_in_ast(base: str, name: str) -> bool:
        if base in exports and name in exports[base]:
            return True
        if f"{base}.{name}" in exports:
            return True
        parts = base.split(".")
        if len(parts) >= 2:
            parent_mod = ".".join(parts[:-1])
            target_obj = parts[-1]
            if parent_mod in exports and target_obj in exports[parent_mod]:
                for mod_key, names in exports.items():
                    if mod_key.endswith(f".{target_obj}") and name in names:
                        return True
        return False

    def exists_in_runtime(base: str, name: str) -> tuple[bool, str]:
        parts = base.split(".")
        for cut in range(len(parts), 0, -1):
            try:
                obj = importlib.import_module(".".join(parts[:cut]))
            except Exception:  # noqa: BLE001
                continue
            try:
                for attr in parts[cut:]:
                    obj = getattr(obj, attr)
            except AttributeError:
                continue
            if hasattr(obj, name):
                return True, ""
            try:
                importlib.import_module(f"{base}.{name}")
                return True, ""
            except Exception:  # noqa: BLE001
                return False, "name missing"
        return False, "base import failed"

    missing_prod: list[dict[str, Any]] = []
    missing_test: list[dict[str, Any]] = []

    for (base, name), sites in sorted(seen_imports.items()):
        if name in MODULE_DUNDER_ATTRS:
            continue
        ok = exists_in_ast(base, name)
        problem = "name missing"
        if not ok and use_importlib:
            ok, problem = exists_in_runtime(base, name)
        elif not ok and base not in exports:
            problem = "base module missing"
        if ok:
            continue
        prod_sites = [s for s in sites if not is_test_path(s.split(":", 2)[1])]
        test_sites = [s for s in sites if is_test_path(s.split(":", 2)[1])]
        entry = {
            "module": base,
            "name": name,
            "problem": problem,
            "sites": prod_sites or test_sites,
        }
        if prod_sites:
            missing_prod.append(entry)
        elif test_sites:
            missing_test.append(entry)

    return missing_prod, missing_test


def _main_called_symbols(path: str) -> set[str]:
    """Return top-level symbol names loaded inside `def main` of `path`."""
    try:
        tree = ast.parse(Path(path).read_text("utf-8", "replace"))
    except (SyntaxError, OSError):
        return set()
    called: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "main"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    called.add(sub.id)
    return called


def compute_m1(
    pkg_root: Path,
    consumers: dict[str, Path],
    left_the_packages: tuple[str, ...] = DEFAULT_LEFT_THE_PACKAGES,
    package_scoped_left: dict[str, set[str]] | None = None,
) -> tuple[dict[str, Any], dict[tuple[str, str], list[str]]]:
    """Compute M1: public symbol reachability and consumer import verification."""
    defs = public_defs(pkg_root)
    internal = sdk_internal_refs(pkg_root)
    prod, test, seen_imports = consumer_symbol_refs(
        consumers, left_the_packages, package_scoped_left
    )
    cli_map = cli_uses(consumers)
    cli_modules: dict[str, Counter[str]] = defaultdict(Counter)
    for repo_mod, count in cli_map.items():
        repo_name, mod_target = repo_mod.split(":", 1)
        cli_modules[mod_target][repo_name] += count
    main_calls_by_path: dict[str, set[str]] = {}

    missing_prod, missing_test = check_consumer_imports(seen_imports, pkg_root)

    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for d in defs:
        name = d["name"]
        mod = d["module"]
        prod_repos = dict(prod.get(name, {}))
        if mod in cli_modules:
            called_in_main = main_calls_by_path.setdefault(
                d["path"], _main_called_symbols(d["path"])
            )
            if name in called_in_main:
                for r_name, r_cnt in cli_modules[mod].items():
                    prod_repos[r_name] = prod_repos.get(r_name, 0) + r_cnt
        test_repos = dict(test.get(name, {}))
        internal_n = sum(
            1
            for f, ln in internal.get(name, [])
            if not (f == d["path"] and d["line"] <= ln <= d["end"])
        )
        if prod_repos:
            reach = "prod_used"
        elif test_repos and not internal_n:
            reach = "test_only"
        elif internal_n:
            reach = "internal_only"
        else:
            reach = "unused"
        counts[reach] += 1
        rows.append(
            {
                **d,
                "internal_refs": internal_n,
                "prod_repos": ";".join(sorted(prod_repos)),
                "prod_refs": sum(prod_repos.values()),
                "test_refs": sum(test_repos.values()),
                "reach": reach,
            }
        )

    total = len(rows)
    prod_used = counts["prod_used"]
    m1 = {
        "prod_used": prod_used,
        "test_only": counts["test_only"],
        "internal_only": counts["internal_only"],
        "unused": counts["unused"],
        "total_public": total,
        "ratio": round(prod_used / total, 4) if total else 0.0,
        "missing_imports": {
            "prod": missing_prod,
            "test": missing_test,
        },
        "cli_uses": cli_map,
        "symbols": rows,
    }
    return m1, seen_imports


def sdk_env_vocabulary(
    pkg_root: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Return (contract_vocab, full_vocab) parsed from runtime/environment.py or runtime_env_contract()."""
    contract_vocab: dict[str, dict[str, str]] = {}
    env_py = pkg_root / "runtime" / "environment.py"
    if env_py.is_file():
        tree = ast.parse(env_py.read_text("utf-8", "replace"))
        members: dict[str, str] = {}
        specs: list[tuple[str, list[str]]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RuntimeEnvKey":
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    ):
                        members[stmt.targets[0].id] = stmt.value.value
            elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "RUNTIME_ENV_SPECS" for t in node.targets
            ):
                if isinstance(node.value, (ast.Tuple, ast.List)):
                    for call in node.value.elts:
                        if not isinstance(call, ast.Call) or not call.args:
                            continue
                        first = call.args[0]
                        member = getattr(first, "attr", None)
                        if not member:
                            continue
                        aliases: list[str] = []
                        if len(call.args) > 1 and isinstance(
                            call.args[1], (ast.Tuple, ast.List)
                        ):
                            aliases = [
                                elt.value
                                for elt in call.args[1].elts
                                if isinstance(elt, ast.Constant)
                                and isinstance(elt.value, str)
                            ]
                        specs.append((member, aliases))
        for member, aliases in specs:
            if member not in members:
                continue
            canon = members[member]
            contract_vocab[canon] = {"kind": "canonical", "canonical": canon}
            for alias in aliases:
                contract_vocab[alias] = {"kind": "alias", "canonical": canon}

    if not contract_vocab:
        full = envinv.sdk_vocabulary()
        contract_vocab = {
            k: v for k, v in full.items() if v.get("kind") in ("canonical", "alias")
        }

    full_vocab = dict(contract_vocab)
    for name, (kind, rel) in SDK_EXTRA_KEYS.items():
        target = pkg_root / rel
        if target.is_file():
            text = target.read_text("utf-8", "replace")
            needle = name if kind == "template" else f'"{name}"'
            if needle in text:
                full_vocab.setdefault(name, {"kind": kind, "canonical": name})
        else:
            full_vocab.setdefault(name, {"kind": kind, "canonical": name})

    return contract_vocab, full_vocab


def compute_m2(pkg_root: Path, consumers: dict[str, Path]) -> dict[str, Any]:
    """Compute M2: runtime_env_contract() key reachability and SDK class distribution."""
    contract_vocab, full_vocab = sdk_env_vocabulary(pkg_root)
    include_manifests = (
        "infra2" in consumers and consumers["infra2"].resolve() == REPO_ROOT.resolve()
    )
    inv = envinv.run(consumers, include_app_manifests=include_manifests)
    services = envinv.load_services() if include_manifests else []
    keys_map, _comps, _edges, _comp_of = envinv.analyze(
        inv,
        envinv.service_groups(services) if services else {},
        vocab=full_vocab,
    )

    aliases_by_canonical: dict[str, list[str]] = defaultdict(list)
    for k_name, meta in contract_vocab.items():
        if meta["kind"] == "alias":
            aliases_by_canonical[meta["canonical"]].append(k_name)

    def _key_occs(target_key: str, scope: str) -> list[Any]:
        k_obj = keys_map.get(target_key)
        occs = k_obj.occ if k_obj and k_obj.occ else []
        return [
            o
            for o in occs
            if o.scope == scope
            and o.repo != SELF_REPO
            and o.role in ("read", "write", "declare")
        ]

    prod_used = 0
    test_only = 0
    unused = 0
    key_rows: list[dict[str, Any]] = []

    for name in sorted(contract_vocab):
        meta = contract_vocab[name]
        k_obj = keys_map.get(name)
        prod_occs = _key_occs(name, "prod")
        test_occs = _key_occs(name, "test")
        alias_prod_occs: list[Any] = []
        if meta["kind"] == "canonical":
            for alias_name in aliases_by_canonical.get(name, []):
                alias_prod_occs.extend(_key_occs(alias_name, "prod"))
        if prod_occs or alias_prod_occs:
            reach = "prod_used"
            prod_used += 1
        elif test_occs:
            reach = "test_only"
            test_only += 1
        else:
            reach = "unused"
            unused += 1
        sdk_class = (
            k_obj.info.get("sdk_class", "C3 SDK names it, nobody uses it")
            if k_obj
            else "C3 SDK names it, nobody uses it"
        )
        key_rows.append(
            {
                "key": name,
                "kind": meta["kind"],
                "canonical": meta["canonical"],
                "reach": reach,
                "sdk_class": sdk_class,
                "prod_occurrences": len(prod_occs) or len(alias_prod_occs),
                "test_occurrences": len(test_occs),
            }
        )

    class_counts = Counter(
        k.info.get("sdk_class", "")
        for k in keys_map.values()
        if k.info.get("sdk_class")
    )
    for r in key_rows:
        if r["key"] not in keys_map:
            class_counts["C3 SDK names it, nobody uses it"] += 1

    total_keys = len(contract_vocab)
    return {
        "prod_used": prod_used,
        "test_only": test_only,
        "unused": unused,
        "total_keys": total_keys,
        "ratio": round(prod_used / total_keys, 4) if total_keys else 0.0,
        "class_counts": dict(sorted(class_counts.items())),
        "keys": key_rows,
    }


def _parse_version_tuple(version: str) -> tuple[int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", version)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def resolve_latest_sdk_version(pkg_root: Path, explicit: str | None = None) -> str:
    """Determine the target latest SDK version from explicit CLI flag, SDK pyproject, or metadata."""
    if explicit:
        return explicit.removeprefix("v")
    pyproject = pkg_root.parent.parent / "pyproject.toml"
    if pyproject.is_file():
        m = re.search(
            r'^version\s*=\s*"([^"]+)"',
            pyproject.read_text("utf-8", "replace"),
            re.M,
        )
        if m:
            return m.group(1).removeprefix("v")
    init_py = pkg_root / "__init__.py"
    if init_py.is_file():
        m = re.search(
            r'__version__\s*=\s*"([^"]+)"',
            init_py.read_text("utf-8", "replace"),
        )
        if m and m.group(1).removeprefix("v") != "0.0.0":
            return m.group(1).removeprefix("v")
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("infra2-sdk").removeprefix("v")
    except Exception:  # noqa: BLE001
        pass
    import infra2_sdk

    return str(getattr(infra2_sdk, "__version__", "0.0.0")).removeprefix("v")


def _extract_sdk_version(text: str) -> str | None:
    m = re.search(
        r"(?<![\w.-])infra2[-_]sdk(?:\[[^\]]*\])?\s*(?:==\s*v?([0-9]+\.[0-9]+\.[0-9]+)"
        r"|@[^\n\"']*?(?:infra2_sdk-|/v|@v|(?<![0-9A-Za-z])v)([0-9]+\.[0-9]+\.[0-9]+))",
        text,
    )
    if m:
        return (m.group(1) or m.group(2)).strip()
    return None


def consumer_pinned_version(root: Path) -> str | None:
    """Extract the pinned infra2-sdk version from a consumer repository."""
    lock_rel_paths = ("uv.lock", "apps/backend/uv.lock")
    for rel in lock_rel_paths:
        lock_path = root / rel
        if lock_path.is_file():
            try:
                req = sdk_requirement(lock_path)
                ver = _extract_sdk_version(req)
                if ver:
                    return ver
            except Exception:  # noqa: BLE001
                pass

    for rel in ("pyproject.toml", "apps/backend/pyproject.toml"):
        pyproj = root / rel
        if not pyproj.is_file():
            continue
        text = pyproj.read_text("utf-8", "replace")
        ver = _extract_sdk_version(text)
        if ver:
            return ver
    return None


def compute_m3(
    consumers: dict[str, Path],
    latest_version: str,
) -> dict[str, Any]:
    """Compute M3: consumer infra2-sdk version pin lag behind latest_version."""
    latest_clean = latest_version.removeprefix("v")
    latest_tuple = _parse_version_tuple(latest_clean)
    per_repo: dict[str, dict[str, Any]] = {}
    lagging_repos: list[str] = []

    for repo, root in sorted(consumers.items()):
        pinned = consumer_pinned_version(root)
        if pinned is None:
            per_repo[repo] = {
                "pinned_version": None,
                "latest_version": latest_clean,
                "up_to_date": False,
                "lag": None,
            }
            lagging_repos.append(repo)
            continue
        pinned_tuple = _parse_version_tuple(pinned)
        up_to_date = pinned_tuple == latest_tuple
        if latest_tuple[0] != pinned_tuple[0]:
            lag = pinned_tuple[0] - latest_tuple[0]
        elif latest_tuple[1] != pinned_tuple[1]:
            lag = pinned_tuple[1] - latest_tuple[1]
        else:
            lag = pinned_tuple[2] - latest_tuple[2]
        per_repo[repo] = {
            "pinned_version": pinned,
            "latest_version": latest_clean,
            "up_to_date": up_to_date,
            "lag": lag,
        }
        if not up_to_date:
            lagging_repos.append(repo)

    return {
        "latest_version": latest_clean,
        "all_up_to_date": len(lagging_repos) == 0,
        "lagging_repos": lagging_repos,
        "consumers": per_repo,
    }


def _iter_removed_name_pairs(tree: ast.Module) -> list[tuple[str, str]]:
    """Extract (module, dotted_name) string pairs from REMOVED_* tuple/list assignments."""
    pairs: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name)
            and (t.id == "REMOVED_NAMES" or t.id.startswith("REMOVED_IN_"))
            for t in node.targets
        ):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            continue
        for elt in node.value.elts:
            if (
                isinstance(elt, (ast.Tuple, ast.List))
                and len(elt.elts) == 2
                and isinstance(elt.elts[0], ast.Constant)
                and isinstance(elt.elts[0].value, str)
                and isinstance(elt.elts[1], ast.Constant)
                and isinstance(elt.elts[1].value, str)
            ):
                pairs.append((elt.elts[0].value, elt.elts[1].value))
    return pairs


def load_left_the_packages(pkg_root: Path | None = None) -> tuple[str, ...]:
    """Return the tuple of unconditionally retired SDK symbols."""
    names = list(DEFAULT_LEFT_THE_PACKAGES)
    if pkg_root is not None:
        tests_dir = pkg_root.parent.parent / "tests"
        readme_test = tests_dir / "test_readme_claims.py"
        if readme_test.is_file():
            try:
                tree = ast.parse(readme_test.read_text("utf-8", "replace"))
                for node in tree.body:
                    if isinstance(node, ast.Assign) and any(
                        getattr(t, "id", "") == "LEFT_THE_PACKAGES"
                        for t in node.targets
                    ):
                        if isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
                            for elt in node.value.elts:
                                if (
                                    isinstance(elt, ast.Constant)
                                    and isinstance(elt.value, str)
                                    and elt.value not in names
                                ):
                                    names.append(elt.value)
                for _mod, dotted in _iter_removed_name_pairs(tree):
                    if (
                        "." not in dotted
                        and dotted not in SKIP_SYMBOL_NAMES
                        and dotted not in names
                    ):
                        names.append(dotted)
            except (SyntaxError, OSError):
                pass
        wheel_smoke = tests_dir / "wheel_smoke.py"
        if wheel_smoke.is_file():
            try:
                tree = ast.parse(wheel_smoke.read_text("utf-8", "replace"))
                for _mod, dotted in _iter_removed_name_pairs(tree):
                    if (
                        "." not in dotted
                        and dotted not in SKIP_SYMBOL_NAMES
                        and dotted not in names
                    ):
                        names.append(dotted)
            except (SyntaxError, OSError):
                pass
    return tuple(names)


def load_package_scoped_left_names(
    pkg_root: Path | None = None,
) -> dict[str, set[str]]:
    """Return module-scoped retired symbol names from dict LEFT_THE_PACKAGES and REMOVED_* tables."""
    scoped: dict[str, set[str]] = defaultdict(set)
    if pkg_root is None:
        return {}
    tests_dir = pkg_root.parent.parent / "tests"
    readme_test = tests_dir / "test_readme_claims.py"
    if readme_test.is_file():
        try:
            tree = ast.parse(readme_test.read_text("utf-8", "replace"))
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "LEFT_THE_PACKAGES" for t in node.targets
                ):
                    if isinstance(node.value, ast.Dict):
                        for pkg_key, mod_dict in zip(
                            node.value.keys, node.value.values, strict=False
                        ):
                            if not (
                                isinstance(pkg_key, ast.Constant)
                                and isinstance(pkg_key.value, str)
                                and isinstance(mod_dict, ast.Dict)
                            ):
                                continue
                            pkg_name = pkg_key.value
                            for sym_list in mod_dict.values:
                                if not isinstance(
                                    sym_list, (ast.List, ast.Tuple, ast.Set)
                                ):
                                    continue
                                for elt in sym_list.elts:
                                    if isinstance(elt, ast.Constant) and isinstance(
                                        elt.value, str
                                    ):
                                        scoped[pkg_name].add(elt.value)
            for mod_name, dotted in _iter_removed_name_pairs(tree):
                if "." in dotted:
                    parent, attr = dotted.rsplit(".", 1)
                    scoped[f"{mod_name}.{parent}"].add(attr)
                else:
                    scoped[mod_name].add(dotted)
        except (SyntaxError, OSError):
            pass
    wheel_smoke = tests_dir / "wheel_smoke.py"
    if wheel_smoke.is_file():
        try:
            tree = ast.parse(wheel_smoke.read_text("utf-8", "replace"))
            for mod_name, dotted in _iter_removed_name_pairs(tree):
                if "." in dotted:
                    parent, attr = dotted.rsplit(".", 1)
                    scoped[f"{mod_name}.{parent}"].add(attr)
                else:
                    scoped[mod_name].add(dotted)
        except (SyntaxError, OSError):
            pass
    return {k: set(v) for k, v in scoped.items()}


def _try_imports_sdk(node: ast.Try) -> bool:
    """Return True when a try block imports infra2_sdk and catches ImportError/ModuleNotFoundError."""
    catches_import_error = False
    for handler in node.handlers:
        if handler.type is None:
            continue
        names: list[str] = []
        if isinstance(handler.type, ast.Name):
            names.append(handler.type.id)
        elif isinstance(handler.type, ast.Tuple):
            names.extend(
                elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)
            )
        if any(n in ("ImportError", "ModuleNotFoundError") for n in names):
            catches_import_error = True
            break
    if not catches_import_error:
        return False

    for stmt in node.body:
        for sub in ast.walk(stmt):
            if (
                isinstance(sub, ast.ImportFrom)
                and sub.module
                and sub.module.split(".")[0] == "infra2_sdk"
            ):
                return True
            if isinstance(sub, ast.Import) and any(
                alias.name.split(".")[0] == "infra2_sdk" for alias in sub.names
            ):
                return True
    return False


def compute_m4(
    seen_imports: dict[tuple[str, str], list[str]],
    left_the_packages: tuple[str, ...],
    package_scoped_left: dict[str, set[str]] | None = None,
    consumers: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Compute M4: consumer references to retired symbols, residual libs/ or tools/_lib/ dirs, and SDK ImportError fallbacks."""
    retired = set(left_the_packages)
    pkg_left = package_scoped_left or {}
    by_repo: dict[str, list[dict[str, str]]] = defaultdict(list)
    sites_all: list[dict[str, str]] = []

    for (module, name), sites in sorted(seen_imports.items()):
        if name not in retired and name not in pkg_left.get(module, set()):
            continue
        for site in sorted(set(sites)):
            repo = site.split(":", 1)[0]
            rec = {"module": module, "name": name, "site": site}
            by_repo[repo].append(rec)
            sites_all.append(rec)

    if consumers:
        for repo, root in sorted(consumers.items()):
            repo_files = list_repo_files(root)
            if any(rel.startswith("libs/") for rel in repo_files):
                rec = {
                    "module": "libs",
                    "name": "libs/",
                    "site": f"{repo}:libs",
                }
                by_repo[repo].append(rec)
                sites_all.append(rec)
            if any(rel.startswith("tools/_lib/") for rel in repo_files):
                rec = {
                    "module": "tools._lib",
                    "name": "tools/_lib/",
                    "site": f"{repo}:tools/_lib",
                }
                by_repo[repo].append(rec)
                sites_all.append(rec)
            for rel in repo_files:
                if (
                    not rel.endswith(".py")
                    or is_test_path(rel)
                    or rel in CENSUS_SELF_REL_PATHS
                ):
                    continue
                try:
                    tree = ast.parse((root / rel).read_text("utf-8", "replace"))
                except (SyntaxError, UnicodeDecodeError, OSError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Try) and _try_imports_sdk(node):
                        rec = {
                            "module": "infra2_sdk",
                            "name": "except_ImportError_fallback",
                            "site": f"{repo}:{rel}:{node.lineno}",
                        }
                        by_repo[repo].append(rec)
                        sites_all.append(rec)
                if (
                    rel.startswith("common/")
                    and not rel.endswith("/__init__.py")
                    and rel
                    not in {
                        "common/__init__.py",
                        "common/runtime/src/truealpha_runtime/boot.py",
                        "common/meta/package_contract.py",
                    }
                    and _is_pure_reexport_shim(tree)
                ):
                    rec = {
                        "module": "common",
                        "name": "pure_reexport_shim",
                        "site": f"{repo}:{rel}:1",
                    }
                    by_repo[repo].append(rec)
                    sites_all.append(rec)

    return {
        "total_violations": len(sites_all),
        "by_repo": {repo: len(items) for repo, items in sorted(by_repo.items())},
        "violations": sites_all,
    }


def _is_pure_reexport_shim(tree: ast.Module) -> bool:
    """True when a non-__init__ module only imports and defines __all__ without any def/class/constants."""
    has_import = False
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
                continue
            has_import = True
            continue
        if isinstance(stmt, ast.Assign) and all(
            isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets
        ):
            continue
        return False
    return has_import


def compute_m5(consumers: dict[str, Path]) -> dict[str, Any]:
    """Compute M5: consumer hardcoded SERVICE_NAME / OTEL key lists outside sdk_vocabulary()."""
    by_repo: dict[str, list[dict[str, str]]] = defaultdict(list)
    violations: list[dict[str, str]] = []

    for repo, root in sorted(consumers.items()):
        for rel in list_repo_files(root):
            if is_test_path(rel) or rel in CENSUS_SELF_REL_PATHS:
                continue
            full_path = root / rel
            if rel.endswith(".py"):
                try:
                    text = full_path.read_text("utf-8", "replace")
                    tree = ast.parse(text)
                except (SyntaxError, UnicodeDecodeError, OSError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
                        strs = {
                            elt.value
                            for elt in node.elts
                            if isinstance(elt, ast.Constant)
                            and isinstance(elt.value, str)
                        }
                    elif isinstance(node, ast.Dict):
                        strs = {
                            k.value
                            for k in node.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        }
                    else:
                        continue
                    if "SERVICE_NAME" in strs and (
                        any(s.startswith("OTEL_") for s in strs)
                        or bool(strs & OTEL_COMPANION_KEYS)
                    ):
                        rec = {
                            "repo": repo,
                            "site": f"{repo}:{rel}:{node.lineno}",
                            "kind": "hardcoded_collection",
                        }
                        by_repo[repo].append(rec)
                        violations.append(rec)
                if "SERVICE_NAME" in text and "OTEL_SERVICE_NAME" in text:
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call) and isinstance(
                            node.func, ast.Attribute
                        ):
                            if node.func.attr in ("get", "getenv", "pop") and node.args:
                                first = node.args[0]
                                if (
                                    isinstance(first, ast.Constant)
                                    and first.value == "SERVICE_NAME"
                                ):
                                    rec = {
                                        "repo": repo,
                                        "site": f"{repo}:{rel}:{node.lineno}",
                                        "kind": "env_lookup",
                                    }
                                    by_repo[repo].append(rec)
                                    violations.append(rec)
                        elif isinstance(node, ast.Subscript):
                            sl = node.slice
                            if (
                                isinstance(sl, ast.Constant)
                                and sl.value == "SERVICE_NAME"
                            ):
                                rec = {
                                    "repo": repo,
                                    "site": f"{repo}:{rel}:{node.lineno}",
                                    "kind": "env_subscript",
                                }
                                by_repo[repo].append(rec)
                                violations.append(rec)
            elif rel.endswith(".json") and (
                "env.manifest" in rel or "required-env" in rel
            ):
                try:
                    raw = full_path.read_text("utf-8", "replace")
                    data = json.loads(raw)
                except (OSError, json.JSONDecodeError):
                    continue
                serialized = json.dumps(data)
                if '"SERVICE_NAME"' in serialized:
                    rec = {
                        "repo": repo,
                        "site": f"{repo}:{rel}:1",
                        "kind": "manifest_service_name",
                    }
                    by_repo[repo].append(rec)
                    violations.append(rec)

    return {
        "total_violations": len(violations),
        "by_repo": {repo: len(items) for repo, items in sorted(by_repo.items())},
        "violations": violations,
    }


def compute_m6(consumers: dict[str, Path]) -> dict[str, Any]:
    """Compute M6: consumer tools/*.py cross-imports, hijacks, load_tool calls, and reverse common->tools imports."""
    by_repo: dict[str, list[dict[str, str]]] = defaultdict(list)
    violations: list[dict[str, str]] = []

    for repo, root in sorted(consumers.items()):
        for rel in list_repo_files(root, suffix=".py"):
            if is_test_path(rel) or rel in CENSUS_SELF_REL_PATHS:
                continue
            in_tools = rel.startswith("tools/")
            in_prod = rel.startswith(("common/", "apps/", "platform/"))
            if not (in_tools or in_prod):
                continue
            try:
                tree = ast.parse((root / rel).read_text("utf-8", "replace"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if in_tools and (node.level or 0) > 0:
                        rec = {
                            "repo": repo,
                            "site": f"{repo}:{rel}:{node.lineno}",
                            "import": f"{'.' * (node.level or 1)}{node.module or ''}",
                        }
                        by_repo[repo].append(rec)
                        violations.append(rec)
                    elif node.module and node.module.split(".")[0] == "tools":
                        rec = {
                            "repo": repo,
                            "site": f"{repo}:{rel}:{node.lineno}",
                            "import": node.module,
                        }
                        by_repo[repo].append(rec)
                        violations.append(rec)
                    elif in_tools and any(alias.name == "*" for alias in node.names):
                        rec = {
                            "repo": repo,
                            "site": f"{repo}:{rel}:{node.lineno}",
                            "import": f"{node.module or ''}.*",
                        }
                        by_repo[repo].append(rec)
                        violations.append(rec)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "tools":
                            rec = {
                                "repo": repo,
                                "site": f"{repo}:{rel}:{node.lineno}",
                                "import": alias.name,
                            }
                            by_repo[repo].append(rec)
                            violations.append(rec)
                elif in_tools and isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Attribute)
                            and isinstance(target.value.value, ast.Name)
                            and target.value.value.id == "sys"
                            and target.value.attr == "modules"
                            and isinstance(target.slice, ast.Name)
                            and target.slice.id == "__name__"
                        ):
                            rec = {
                                "repo": repo,
                                "site": f"{repo}:{rel}:{node.lineno}",
                                "import": "sys.modules[__name__]",
                            }
                            by_repo[repo].append(rec)
                            violations.append(rec)
                elif isinstance(node, ast.Call):
                    if (
                        isinstance(node.func, ast.Name) and node.func.id == "load_tool"
                    ) or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "load_tool"
                    ):
                        rec = {
                            "repo": repo,
                            "site": f"{repo}:{rel}:{node.lineno}",
                            "import": "load_tool()",
                        }
                        by_repo[repo].append(rec)
                        violations.append(rec)

    return {
        "total_violations": len(violations),
        "passed": len(violations) == 0,
        "by_repo": {repo: len(items) for repo, items in sorted(by_repo.items())},
        "violations": violations,
    }


def write_artifacts(out_dir: Path, report: dict[str, Any]) -> None:
    """Write CSV and JSON census artifacts when --out is specified."""
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols = report["metrics"]["M1"]["symbols"]
    if symbols:
        with (out_dir / "sdk_api_reach.csv").open(
            "w", newline="", encoding="utf-8"
        ) as fh:
            writer = csv.DictWriter(fh, fieldnames=list(symbols[0]))
            writer.writeheader()
            writer.writerows(symbols)

    keys = report["metrics"]["M2"]["keys"]
    if keys:
        with (out_dir / "sdk_cover.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(keys[0]))
            writer.writeheader()
            writer.writerows(keys)

    (out_dir / "sdk_usage_census.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_census(
    pkg_root: Path | None = None,
    consumers: dict[str, Path] | None = None,
    latest_version: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute the full M1 through M6 usage census and return the structured report."""
    resolved_pkg = resolve_sdk_pkg_root(pkg_root)
    resolved_consumers = (
        dict(consumers) if consumers is not None else default_consumers(REPO_ROOT)
    )
    target_version = resolve_latest_sdk_version(resolved_pkg, latest_version)
    left_the_packages = load_left_the_packages(resolved_pkg)
    package_scoped_left = load_package_scoped_left_names(resolved_pkg)

    m1, seen_imports = compute_m1(
        resolved_pkg,
        resolved_consumers,
        left_the_packages,
        package_scoped_left,
    )
    m2 = compute_m2(resolved_pkg, resolved_consumers)
    m3 = compute_m3(resolved_consumers, target_version)
    m4 = compute_m4(
        seen_imports,
        left_the_packages,
        package_scoped_left,
        resolved_consumers,
    )
    m5 = compute_m5(resolved_consumers)
    m6 = compute_m6(resolved_consumers)

    report: dict[str, Any] = {
        "sdk_package": str(resolved_pkg),
        "latest_version": target_version,
        "consumers": sorted(resolved_consumers),
        "metrics": {
            "M1": m1,
            "M2": m2,
            "M3": m3,
            "M4": m4,
            "M5": m5,
            "M6": m6,
        },
    }
    if out_dir is not None:
        write_artifacts(out_dir, report)
    return report


def evaluate_checks(
    report: dict[str, Any],
    *,
    check_test_imports: bool = False,
) -> list[str]:
    """Evaluate census guard conditions across M1 through M6 and return failure messages."""
    failures: list[str] = []
    metrics = report["metrics"]

    m1 = metrics["M1"]
    enforce_full_reachability = len(report.get("consumers", [])) >= 2
    if m1["total_public"] <= 0:
        failures.append("M1: zero public SDK symbols discovered")
    if enforce_full_reachability and (
        m1.get("test_only", 0) > 0
        or m1.get("internal_only", 0) > 0
        or m1.get("unused", 0) > 0
    ):
        non_prod = [
            f"{s['module']}.{s['name']} ({s['reach']})"
            for s in m1.get("symbols", [])
            if s.get("reach") != "prod_used"
        ]
        samples = ", ".join(non_prod[:5])
        failures.append(
            f"M1: non-production SDK symbols (test_only={m1.get('test_only', 0)}, "
            f"internal_only={m1.get('internal_only', 0)}, unused={m1.get('unused', 0)}): {samples}"
        )
    missing_prod = m1["missing_imports"]["prod"]
    if missing_prod:
        samples = ", ".join(
            f"{item['module']}.{item['name']} ({item['sites'][0]})"
            for item in missing_prod[:5]
        )
        failures.append(f"M1: missing production SDK imports: {samples}")
    if check_test_imports and m1["missing_imports"]["test"]:
        samples = ", ".join(
            f"{item['module']}.{item['name']} ({item['sites'][0]})"
            for item in m1["missing_imports"]["test"][:5]
        )
        failures.append(f"M1: missing test SDK imports: {samples}")

    m2 = metrics["M2"]
    if m2["total_keys"] <= 0:
        failures.append("M2: zero runtime_env_contract() keys discovered")
    if m2["prod_used"] <= 0:
        failures.append("M2: zero runtime_env_contract() keys used in production")
    elif m2.get("test_only", 0) > 0 or m2.get("unused", 0) > 0:
        non_prod_keys = [
            f"{k['key']} ({k['reach']})"
            for k in m2.get("keys", [])
            if k.get("reach") != "prod_used"
        ]
        samples = ", ".join(non_prod_keys[:5])
        failures.append(
            f"M2: non-production runtime_env_contract() keys (test_only={m2.get('test_only', 0)}, "
            f"unused={m2.get('unused', 0)}): {samples}"
        )

    m3 = metrics["M3"]
    if not m3["all_up_to_date"]:
        details = ", ".join(
            f"{repo}={info['pinned_version']} (expected {info['latest_version']})"
            for repo, info in sorted(m3["consumers"].items())
            if not info["up_to_date"]
        )
        failures.append(f"M3: consumer SDK version pin lag: {details}")

    m4 = metrics["M4"]
    if m4["total_violations"] > 0:
        samples = ", ".join(f"{v['name']}@{v['site']}" for v in m4["violations"][:5])
        failures.append(
            f"M4: {m4['total_violations']} reference(s) to LEFT_THE_PACKAGES or residual shims: {samples}"
        )

    m5 = metrics["M5"]
    if m5["total_violations"] > 0:
        samples = ", ".join(f"{v['site']} ({v['kind']})" for v in m5["violations"][:5])
        failures.append(
            f"M5: {m5['total_violations']} hardcoded SERVICE_NAME/OTEL list(s) outside sdk_vocabulary(): {samples}"
        )

    m6 = metrics.get("M6")
    if m6 and m6["total_violations"] > 0:
        samples = ", ".join(
            f"{v['site']} ({v['import']})" for v in m6["violations"][:5]
        )
        failures.append(
            f"M6: {m6['total_violations']} tools/*.py cross-import violation(s): {samples}"
        )

    return failures


def _parse_repo_arg(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"Expected NAME=PATH, got {raw!r}")
    name, path_str = raw.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError(f"--repo name must be non-empty, got {raw!r}")
    resolved = Path(path_str).resolve()
    if not resolved.is_dir():
        raise argparse.ArgumentTypeError(
            f"--repo directory does not exist: {path_str!r}"
        )
    return name, resolved


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser for sdk_usage_census."""
    parser = argparse.ArgumentParser(
        description="Compute infra2-sdk usage census metrics M1 through M6."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the census report as JSON on stdout.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail with exit code 1 when any census guard (M1 through M6) fails.",
    )
    parser.add_argument(
        "--tests",
        action="store_true",
        help="Also fail --check when consumer test code imports a missing SDK symbol.",
    )
    parser.add_argument(
        "--sdk-src",
        "--sdk",
        type=Path,
        dest="sdk_src",
        default=None,
        help="Path to the infra2_sdk package or repository root.",
    )
    parser.add_argument(
        "--infra2",
        type=Path,
        default=None,
        help="Path to infra2 repository root.",
    )
    parser.add_argument(
        "--finance-report",
        type=Path,
        default=None,
        help="Path to finance_report repository root.",
    )
    parser.add_argument(
        "--truealpha",
        type=Path,
        default=None,
        help="Path to truealpha repository root.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Consumer repository mapping (repeatable). Defaults to available workspace repos.",
    )
    parser.add_argument(
        "--latest-version",
        default=None,
        help="Expected latest SDK version (defaults to SDK pyproject.toml or installed version).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional directory to write CSV and JSON census artifacts.",
    )
    return parser


def _format_human_summary(report: dict[str, Any], failures: list[str]) -> str:
    m = report["metrics"]
    m1, m2, m3, m4, m5 = m["M1"], m["M2"], m["M3"], m["M4"], m["M5"]
    m6 = m.get("M6", {"total_violations": 0})
    lines = [
        f"SDK package: {report['sdk_package']} (target v{report['latest_version']})",
        f"Consumers:   {', '.join(report['consumers'])}",
        (
            f"M1 symbol reachability: {m1['prod_used']}/{m1['total_public']} "
            f"({m1['ratio'] * 100:.1f}%) "
            f"[test_only={m1['test_only']}, internal_only={m1['internal_only']}, unused={m1['unused']}, "
            f"missing_prod={len(m1['missing_imports']['prod'])}]"
        ),
        (
            f"M2 env key reachability: {m2['prod_used']}/{m2['total_keys']} "
            f"({m2['ratio'] * 100:.1f}%) "
            f"[test_only={m2['test_only']}, unused={m2['unused']}]"
        ),
        "M3 consumer pins: "
        + ", ".join(
            f"{repo}={info['pinned_version']} (lag={info['lag']})"
            for repo, info in sorted(m3["consumers"].items())
        ),
        f"M4 LEFT_THE_PACKAGES / shim references: {m4['total_violations']}",
        f"M5 hardcoded SERVICE_NAME/OTEL lists: {m5['total_violations']}",
        f"M6 tools/*.py cross-imports: {m6['total_violations']}",
    ]
    if failures:
        lines.append("CHECK FAILURES:")
        for fail in failures:
            lines.append(f"  - {fail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for sdk_usage_census."""
    parser = build_parser()
    args = parser.parse_args(argv)

    consumers: dict[str, Path] | None = None
    named_repos = {
        "infra2": args.infra2,
        "finance_report": args.finance_report,
        "truealpha": args.truealpha,
    }
    if args.repo or any(named_repos.values()):
        consumers = {}
        for k, v in named_repos.items():
            if v:
                consumers[k] = v.resolve()
        if args.repo:
            try:
                consumers.update(dict(_parse_repo_arg(item) for item in args.repo))
            except argparse.ArgumentTypeError as exc:
                parser.error(str(exc))

    try:
        report = run_census(
            pkg_root=args.sdk_src,
            consumers=consumers,
            latest_version=args.latest_version,
            out_dir=args.out,
        )
    except FileNotFoundError as exc:
        parser.error(str(exc))

    failures = evaluate_checks(report, check_test_imports=args.tests)
    report["check"] = {
        "passed": len(failures) == 0,
        "failures": failures,
    }

    if args.as_json:
        summary_payload = dict(report)
        print(json.dumps(summary_payload, indent=2, sort_keys=True))
    else:
        print(_format_human_summary(report, failures))

    if args.check and failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
