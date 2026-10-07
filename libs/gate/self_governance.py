"""Self-adjudication, dependency closure computation, and rule drift detection."""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import PurePosixPath

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
    "libs/gate/production_contract.py": "libs/tests/test_production_environment_gate.py",
}


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
                _TESTS_BY_MODULE.get(member),
            ):
                if test and (root / test).is_file():
                    closure.add(test)
    closure |= set(RULE_TEXT_FILES)
    closure |= set(_all_workflow_files())
    if seed not in closure:
        closure = {seed, *RULE_TEXT_FILES}
    return frozenset(closure)


def is_self_governing(path: str) -> bool:
    """True when changes to path would evaluate under self-adjudication rules."""
    return path.startswith(WORKFLOW_PREFIX) or path in self_governing_files()


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
            local = _blob_sha((root / path).read_bytes())
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
