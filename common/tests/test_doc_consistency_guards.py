"""Doc-consistency guards: fail locally (in seconds) the instant a referenced doc/AC drifts,
instead of at CI-runtime in whatever test happens to read it.

Two drift surfaces these close:
1. Code -> doc PATH references (test_*.py reading `docs/.../x.md`): a moved/deleted doc must
   fail here, not only when that specific content-proof test runs.
2. docs -> AC -> test traceability (`Infra-XX.Y`): a test citing an AC id that no EPIC defines
   (typo / renamed / deleted EPIC) is a silent orphan today — there is no registry guard.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).name
# tools/doc_link_check.py is the one file whose job is to RECORD paths that do
# not resolve: its KNOWN_UNRESOLVED map holds each dead cross-reference the
# repository has deliberately not guessed a target for, together with the
# decision that is pending. Scanning it here would read those records as
# references and fail on exactly the thing they exist to track. Excluded for
# the same reason THIS is -- a file that talks about broken paths is not a
# file that has broken paths.
_RECORDS_UNRESOLVED_PATHS = {"doc_link_check.py"}

# Scope to the GOVERNED doc trees (SSOT + EPIC). Arbitrary made-up paths used as test
# fixtures (e.g. `docs/notes.md` "owned by nobody -> dropped") live outside these subtrees.
_DOC_PATH_RE = re.compile(r"docs/(?:ssot|project)/[\w./-]+\.(?:md|ya?ml)")
_AC_RE = re.compile(r"Infra-0\d+\.\d+")
# concrete (non-glob, no-space) file paths cited in backticks, with a known source extension.
# Includes md + ctmpl: EPIC AC rows cite doc and secret-template proofs too (e.g. Infra-014).
_PROOF_PATH_RE = re.compile(r"`([\w./-]+\.(?:py|ya?ml|js|json|sh|hcl|md|ctmpl))`")
# A proof path is checkable only if it's repo-relative under a known infra2 top-level dir —
# bare filenames (`infra-ci.yml`) are ambiguous and cross-repo paths (`common/...` is the app
# repo) can't be resolved here, so they are out of scope rather than false failures.
_INFRA2_PREFIX_RE = re.compile(
    r"^(?:common|tools|platform|bootstrap|docs|cloudflare|finance_report|scripts|\.github)/"
)


def _code_py_files() -> list[Path]:
    files: list[Path] = []
    for base in ("common", "tools", "platform", "bootstrap"):
        files.extend((ROOT / base).rglob("*.py"))
    return [
        f
        for f in files
        if f.name != THIS
        and f.name not in _RECORDS_UNRESOLVED_PATHS
        and "__pycache__" not in f.parts
    ]


def test_code_doc_path_references_resolve() -> None:
    """Every `docs/**/*.md|.yaml` path literal in code/tests must resolve to a real file."""
    missing: list[str] = []
    for path in _code_py_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for ref in set(_DOC_PATH_RE.findall(text)):
            if not (ROOT / ref).exists():
                missing.append(f"{path.relative_to(ROOT)} -> {ref}")
    assert not missing, "doc paths referenced in code do not resolve:\n" + "\n".join(
        sorted(missing)
    )


def _defined_ac_ids() -> set[str]:
    defined: set[str] = set()
    # rglob so archived EPICs under docs/project/archive/ count as defining their ACs too —
    # else a test citing an archived AC would falsely look orphaned.
    for path in (ROOT / "docs/project").rglob("Infra-*.md"):
        defined |= set(
            _AC_RE.findall(path.read_text(encoding="utf-8", errors="ignore"))
        )
    return defined


def test_ac_ids_cited_in_tests_are_defined_in_an_epic() -> None:
    """Every `Infra-XX.Y` AC id cited in a test must be DEFINED in some EPIC doc — so a typo or
    a citation to a renamed/removed AC fails loudly instead of orphaning silently."""
    defined = _defined_ac_ids()
    assert defined, "no AC ids found in docs/project/Infra-*.md (parser drift?)"

    orphans: dict[str, list[str]] = {}
    for path in (ROOT / "common/tests").rglob("test_*.py"):
        if path.name == THIS:
            continue
        for ac in set(
            _AC_RE.findall(path.read_text(encoding="utf-8", errors="ignore"))
        ):
            if ac not in defined:
                orphans.setdefault(ac, []).append(path.name)
    assert not orphans, f"tests cite AC ids not defined in any EPIC: {orphans}"


def test_ac_proof_file_paths_in_epics_exist() -> None:
    """Concrete (non-glob) proof file paths cited in EPIC AC tables must exist — no proof-rot
    where an AC points at a renamed/deleted test or tool."""
    missing: list[str] = []
    for doc in (ROOT / "docs/project").rglob("Infra-*.md"):  # incl. archive/
        for line in doc.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not _AC_RE.search(line):
                continue  # only AC rows carry proof references
            for ref in _PROOF_PATH_RE.findall(line):
                if "*" in ref or not _INFRA2_PREFIX_RE.match(ref):
                    continue  # globs + bare/cross-repo paths are out of scope (see above)
                if not (ROOT / ref).exists():
                    missing.append(f"{doc.name}: {ref}")
    assert not missing, "EPIC AC proof paths do not resolve:\n" + "\n".join(
        sorted(missing)
    )


_SDK_DOTTED_RE = re.compile(r"\binfra2[_-]sdk(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _active_governed_docs() -> list[Path]:
    docs: list[Path] = []
    for top in ("init.md", "README.md", "SECRETS.md"):
        p = ROOT / top
        if p.is_file():
            docs.append(p)
    for sub in ("docs", "common", "tools", "platform", "bootstrap", "harness"):
        base = ROOT / sub
        if not base.is_dir():
            continue
        for ext in ("*.md", "*.yaml", "*.yml"):
            for p in base.rglob(ext):
                if "archive" in p.parts:
                    continue
                docs.append(p)
    return sorted(set(docs))


def _resolve_sdk_dotted_reference(ref: str) -> str | None:
    """Return an error string if `ref` is invalid or cannot resolve in `infra2_sdk`, else None."""
    if ref.startswith("infra2-sdk."):
        return f"invalid hyphenated package prefix in {ref!r} (use 'infra2_sdk.')"
    parts = ref.split(".")
    for i in range(len(parts), 0, -1):
        mod_cand = ".".join(parts[:i])
        try:
            obj: object = importlib.import_module(mod_cand)
            for attr in parts[i:]:
                if not hasattr(obj, attr):
                    return f"{ref!r}: {mod_cand!r} has no attribute {attr!r}"
                obj = getattr(obj, attr)
            return None
        except Exception:
            continue
    return f"{ref!r}: could not import module from {ref!r}"


def test_active_docs_infra2_sdk_dotted_references_resolve() -> None:
    """Every `infra2_sdk.<path>` reference in active docs must resolve to a real module/symbol."""
    errors: list[str] = []
    checked = 0
    for doc in _active_governed_docs():
        text = doc.read_text(encoding="utf-8", errors="ignore")
        for ref in sorted(set(_SDK_DOTTED_RE.findall(text))):
            checked += 1
            err = _resolve_sdk_dotted_reference(ref)
            if err is not None:
                errors.append(f"{doc.relative_to(ROOT)}: {err}")
    assert checked >= 15, (
        f"expected >= 15 infra2_sdk references across docs, got {checked}"
    )
    assert not errors, "unresolved infra2_sdk doc references:\n" + "\n".join(errors)


def test_sdk_dotted_reference_resolver_rejects_hyphen_and_missing_attr() -> None:
    assert _resolve_sdk_dotted_reference("infra2_sdk.secrets") is None
    assert (
        _resolve_sdk_dotted_reference(
            "infra2_sdk.observability.evaluate_consecutive_hysteresis"
        )
        is None
    )
    assert "hyphenated" in (_resolve_sdk_dotted_reference("infra2-sdk.secrets") or "")
    assert (
        _resolve_sdk_dotted_reference("infra2_sdk.ci.coverage_regression") is not None
    )


def _module_ast_defined_names(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                defined.add(alias.asname or alias.name)
    return defined


def _check_domain_readme_module_map(readme: Path) -> list[str]:
    pkg_dir = readme.parent
    rel_label = (
        str(readme.relative_to(ROOT)) if readme.is_relative_to(ROOT) else readme.name
    )
    text = readme.read_text(encoding="utf-8")
    errors: list[str] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 3 or cells[0] in ("Module", "--------", "---"):
            continue
        mod_match = re.search(r"`([A-Za-z0-9_./]+\.py)`", cells[0])
        if not mod_match:
            continue
        mod_rel = mod_match.group(1)
        py_file = pkg_dir / mod_rel
        if not py_file.is_file():
            errors.append(f"{rel_label}: missing module {mod_rel}")
            continue
        defined = _module_ast_defined_names(py_file)
        for raw_tok in re.findall(r"`([^`]+)`", cells[2]):
            clean = raw_tok.removesuffix("()")
            if "." in clean:
                clean = clean.split(".")[0]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean):
                continue
            if clean not in defined:
                errors.append(f"{rel_label} ({mod_rel}): symbol {clean!r} not in AST")
    return errors


def test_common_domain_readme_module_maps_match_ast_exports() -> None:
    """Every `common/<domain>/README.md` Module Map row must list real `.py` files and AST symbols."""
    readmes = sorted((ROOT / "common").glob("*/README.md"))
    assert len(readmes) >= 5
    errors: list[str] = []
    for readme in readmes:
        errors.extend(_check_domain_readme_module_map(readme))
    assert not errors, "common/*/README.md Module Map drift:\n" + "\n".join(errors)


def test_domain_readme_module_map_guard_rejects_phantom_export(tmp_path: Path) -> None:
    mod = tmp_path / "sample.py"
    mod.write_text("def real_fn() -> int:\n    return 1\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(
        "| Module | Role | Key Exports |\n|---|---|---|\n| `sample.py` | role | `real_fn()`, `ghost_fn()` |\n",
        encoding="utf-8",
    )
    errs = _check_domain_readme_module_map(readme)
    assert any("ghost_fn" in e for e in errs)


_DOTTED_REPO_REF_RE = re.compile(r"`((?:common|tools)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")
_TEST_FILE_REF_RE = re.compile(r"`(common/tests/test_[A-Za-z0-9_]+\.py)`")


def _check_python_code_block_imports(text: str, label: str) -> list[str]:
    errors: list[str] = []
    for m in re.finditer(r"```python\n(.*?)```", text, re.S):
        try:
            tree = ast.parse(m.group(1))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if not node.module.startswith(("common", "tools", "infra2_sdk")):
                    continue
                try:
                    mod = importlib.import_module(node.module)
                except Exception as exc:
                    errors.append(f"{label}: cannot import {node.module!r} ({exc})")
                    continue
                for alias in node.names:
                    if alias.name != "*" and not hasattr(mod, alias.name):
                        errors.append(
                            f"{label}: {node.module!r} has no attribute {alias.name!r}"
                        )
    return errors


def _resolve_dotted_repo_ref(ref: str) -> str | None:
    if ref.endswith((".py", ".md", ".yaml", ".yml", ".json", ".sh", ".hcl", ".ctmpl")):
        return None
    parts = ref.split(".")
    for i in range(len(parts), 0, -1):
        mod_cand = ".".join(parts[:i])
        try:
            obj: object = importlib.import_module(mod_cand)
            for attr in parts[i:]:
                if not hasattr(obj, attr):
                    return f"{ref!r}: {mod_cand!r} has no attribute {attr!r}"
                obj = getattr(obj, attr)
            return None
        except Exception:
            continue
    return f"{ref!r}: could not resolve module or attribute"


def test_active_docs_python_imports_dotted_refs_and_test_paths_resolve() -> None:
    """Every active doc python import, dotted common/tools ref, and common/tests path resolves."""
    errors: list[str] = []
    for doc in _active_governed_docs():
        if doc.suffix != ".md":
            continue
        rel = str(doc.relative_to(ROOT))
        text = doc.read_text(encoding="utf-8", errors="ignore")
        errors.extend(_check_python_code_block_imports(text, rel))
        for m in _DOTTED_REPO_REF_RE.finditer(text):
            err = _resolve_dotted_repo_ref(m.group(1))
            if err is not None:
                errors.append(f"{rel}: {err}")
        for m in _TEST_FILE_REF_RE.finditer(text):
            if not (ROOT / m.group(1)).is_file():
                errors.append(f"{rel}: missing test file {m.group(1)!r}")
    assert not errors, "active doc import/symbol/test-path drift:\n" + "\n".join(errors)


def test_python_import_and_dotted_repo_ref_guards_reject_invalid_symbols() -> None:
    bad_md = "```python\nfrom common.observability.issue_trail import GhIssueApi\n```\n"
    assert any(
        "GhIssueApi" in e for e in _check_python_code_block_imports(bad_md, "README.md")
    )
    assert (
        _resolve_dotted_repo_ref(
            "common.deploy.deployer.Deployer._assert_approle_creds_present"
        )
        is None
    )
    assert (
        _resolve_dotted_repo_ref("common.deploy.promote.assert_approle_creds_present")
        is None
    )
    assert (
        _resolve_dotted_repo_ref("common.deploy.deployer._assert_approle_creds_present")
        is not None
    )
