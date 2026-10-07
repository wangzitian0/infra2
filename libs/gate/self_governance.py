"""Self-adjudication, dependency closure computation, and rule drift detection."""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib.machinery
import json
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from libs.gate.inventory import DIRECTION_PROOFS, _workflow_only_gained_authority
from libs.gate.types import (
    RULE_TEXT_FILES,
    WORKFLOW_PREFIX,
    Runner,
    _get_root,
    _get_workflow_dir,
)

_OWNER_INSTRUCTION_HEADER_RE = re.compile(
    r"(?im)^(##+\s*)?(owner instruction|owner 指示)\b.*$"
)
_QUOTE_LINE_RE = re.compile(r"^>.*[^\W_]|「[^」]*[^\W_][^」]*」")


def _owner_instruction_quoted(body: str) -> bool:
    """True when the PR body cites the owner instruction, not just names it."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if not _OWNER_INSTRUCTION_HEADER_RE.match(line):
            continue
        for later in lines[i + 1 :]:
            stripped = later.strip()
            if not stripped:
                continue
            if _QUOTE_LINE_RE.search(stripped):
                return True
            break
    return False


_DATA_SUFFIXES = (".json", ".yaml", ".yml", ".md")

# Tests whose file name does not follow libs/tests/test_<module stem>.py, by module.
_TESTS_BY_MODULE = {
    "libs/gate/production_contract.py": (
        "libs/tests/test_production_environment_gate.py",
    ),
    "libs/gate/evaluator.py": ("libs/tests/test_gate_pipeline.py",),
    "libs/gate/self_governance.py": ("libs/tests/test_gate_self_governance.py",),
}

# Package files that Python runs before the gate code. They are in the closure even
# when they do not exist: a PR can add one, and `python -m tools.pr_merge_gate` runs it.
PACKAGE_FILES = ("libs/__init__.py", "tools/__init__.py")

# pytest runs each conftest.py on the way to a test. One in libs/tests can switch off
# the gate tests, for example with `collect_ignore_glob` (#1138).
GATE_TESTS_DIR = "libs/tests"

# File name endings that Python imports from a directory on its path.
_IMPORT_SUFFIXES = tuple(importlib.machinery.all_suffixes())


def _repo_deps(rel: str) -> set[str]:
    """Repository files this module imports or reads, one level deep."""
    root = _get_root()
    found: set[str] = set()
    path = root / rel
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return found
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                for name in names:
                    stem = name.replace(".", "/")
                    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                        if (root / candidate).is_file():
                            found.add(candidate)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    if node.module:
                        sub = node.module.replace(".", "/")
                        target = base / sub
                    else:
                        target = base
                    for alias in node.names:
                        for cand in (
                            target / f"{alias.name}.py",
                            target / alias.name / "__init__.py",
                            target.with_suffix(".py"),
                            target / "__init__.py",
                        ):
                            if cand.is_file():
                                try:
                                    found.add(str(cand.relative_to(root)))
                                except ValueError:
                                    pass
                elif node.module:
                    stem = node.module.replace(".", "/")
                    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                        if (root / candidate).is_file():
                            found.add(candidate)
    for match in re.finditer(r"""["']((?:docs|libs|tools)/[\w./-]+)["']""", source):
        rel_data = match.group(1)
        if rel_data.endswith(_DATA_SUFFIXES) and (root / rel_data).is_file():
            found.add(rel_data)
    return found


@functools.lru_cache(maxsize=1)
def self_governing_files() -> frozenset[str]:
    """Everything a change to which would let this gate judge its own rewrite."""
    root = _get_root()
    seed = "tools/pr_merge_gate.py"
    closure = {seed}
    frontier = {seed}
    while frontier:
        nxt: set[str] = set()
        for member in frontier:
            if member.endswith(".py"):
                nxt |= _repo_deps(member)
        nxt -= closure
        closure |= nxt
        frontier = nxt
    for member in list(closure):
        if member.endswith(".py"):
            for test in (
                f"libs/tests/test_{PurePosixPath(member).stem}.py",
                *_TESTS_BY_MODULE.get(member, ()),
            ):
                if (root / test).is_file():
                    closure.add(test)
    # Python runs the __init__.py of each parent package before a module (#1138).
    for member in list(closure):
        if member.endswith(".py"):
            for parent in PurePosixPath(member).parents:
                package_file = f"{parent}/__init__.py"
                if parent != PurePosixPath(".") and (root / package_file).is_file():
                    closure.add(package_file)
    closure |= set(PACKAGE_FILES)
    closure |= {f for f in (f"{GATE_TESTS_DIR}/conftest.py",) if (root / f).is_file()}
    closure |= set(RULE_TEXT_FILES)
    closure |= set(_all_workflow_files())
    if seed not in closure:
        closure = {seed, *RULE_TEXT_FILES}
    return frozenset(closure)


def is_self_governing(path: str) -> bool:
    """True when changes to path would evaluate under self-adjudication rules."""
    return (
        path.startswith(WORKFLOW_PREFIX)
        or path in self_governing_files()
        or _shadows_the_gate(path)
    )


@functools.lru_cache(maxsize=4)
def _closure_shape(
    root: str, closure: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """The directories of the closure's Python files, and the top-level names that
    those files import. Keyed by its inputs, so a test with another root cannot leave
    a stale answer."""
    directories: set[str] = set()
    imported: set[str] = set()
    for member in closure:
        if not member.endswith(".py"):
            continue
        directories.add(str(PurePosixPath(member).parent))
        try:
            tree = ast.parse((Path(root) / member).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imported.add(node.module.split(".")[0])
    return frozenset(directories), frozenset(imported)


def _shadows_the_gate(path: str) -> bool:
    """True for a file that Python or pytest would run instead of, or before, a part of
    the gate (#1138). A rule over the path, so a file a PR adds is caught before it
    exists."""
    pure = PurePosixPath(path)
    importable = pure.name.endswith(_IMPORT_SUFFIXES)
    import_name = pure.name.split(".")[0]
    if len(pure.parts) == 1 and importable:
        return True  # a module at the repository root, such as yaml.py or setup.py
    if len(pure.parts) == 2 and pure.name == "__init__.py":
        return True  # a package at the repository root, such as tools/__init__.py
    if pure.name == "conftest.py" and (
        path.startswith(f"{GATE_TESTS_DIR}/")
        or PurePosixPath(GATE_TESTS_DIR).is_relative_to(pure.parent)
    ):
        return True
    closure = self_governing_files()
    if pure.name in ("__init__.py", "__main__.py") and f"{pure.parent}.py" in closure:
        return True  # a package that replaces a closure module
    if importable and f"{pure.parent}/{import_name}.py" in closure:
        return True  # another form of a closure module, such as types.abi3.so
    directories, imported = _closure_shape(str(_get_root()), closure)
    return importable and str(pure.parent) in directories and import_name in imported


def _all_workflow_files() -> list[str]:
    """Every workflow under .github/workflows, relative to repo root."""
    workflow_dir = _get_workflow_dir()
    if not workflow_dir.is_dir():
        return []
    return sorted(
        f".github/workflows/{p.name}"
        for p in list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))
    )


def _direction_proof_for(path: str):
    """Direction proof for path, or None."""
    if path in DIRECTION_PROOFS:
        return DIRECTION_PROOFS[path]
    if path.startswith(".github/workflows/"):
        return _workflow_only_gained_authority
    return None


def _blob_sha(data: bytes) -> str:
    """A git blob id, computed locally so comparing a tree costs no extra API calls."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _working_tree_rule_drift(
    repo: str, base: str, *, gh: Runner | None = None
) -> tuple[str, ...]:
    """Self-governing files whose working-tree copy differs from base branch."""
    if gh is None:
        from libs.gate.client import _gh

        gh = _gh
    root = _get_root()
    unknown = ("<the base branch's rules could not be read>",)
    if not (repo and base):
        return unknown
    try:
        payload = json.loads(
            gh(
                [
                    "api",
                    f"repos/{repo}/git/trees/{base}?recursive=1",
                    "--jq",
                    "{truncated:.truncated, tree:[.tree[]|{path,sha}]}",
                ]
            )
        )
        if payload.get("truncated"):
            return unknown
        remote = {str(e["path"]): str(e["sha"]) for e in payload.get("tree") or []}
    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError):
        return unknown
    if not remote:
        return unknown
    drift: list[str] = []
    for path in sorted(self_governing_files()):
        try:
            local: str | None = _blob_sha((root / path).read_bytes())
        except FileNotFoundError:
            local = None  # absent here: drift only when the base branch has it
        except OSError:
            drift.append(path)
            continue
        if remote.get(path) != local:
            drift.append(path)
    return tuple(drift)


def _proven_tighter(
    repo: str,
    base: str,
    head_sha: str,
    files: Sequence[str],
    *,
    gh: Runner | None = None,
) -> tuple[str, ...]:
    """Self-governing files in `files` whose change is provably non-loosening."""
    if gh is None:
        from libs.gate.client import _file_at

        file_at = _file_at
    else:
        from libs.gate.client import _file_at

        file_at = functools.partial(_file_at, gh=gh)

    proven: list[str] = []
    for path in sorted(set(files) & self_governing_files()):
        proof = _direction_proof_for(path)
        if proof is None:
            continue
        base_text = file_at(repo, base, path)
        head_text = file_at(repo, head_sha, path)
        if base_text is None or head_text is None:
            continue
        if proof(base_text, head_text):
            proven.append(path)
    return tuple(proven)
