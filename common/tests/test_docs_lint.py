"""Blocking docs-structure lint: each check fails when a doc reference or index drifts.

Every check asserts that the set it scans is not empty, so a check cannot pass on nothing.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from common.gate.production_contract import UNCLASSIFIED_SCHEDULED_JOBS

ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).name
SSOT_DIR = ROOT / "docs/ssot"
PROJECT_DIR = ROOT / "docs/project"
ARCHIVE_INDEX = PROJECT_DIR / "archive/README.md"
MANIFEST = SSOT_DIR / "MANIFEST.yaml"
MKDOCS = ROOT / "docs/mkdocs.yml"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _strip_anchor(path: str) -> str:
    return path.split("#", 1)[0].split("::", 1)[0]


# --- Generated indexes: committed output must equal generator output. ---


@pytest.mark.parametrize("generator", ["gen_project_index", "gen_ssot_index"])
def test_generated_index_equals_committed(generator: str) -> None:
    """The generator check mode exits 1 when the committed index is stale."""
    script = ROOT / "tools" / f"{generator}.py"
    assert script.is_file()
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"Index is stale. Run: python tools/{generator}.py --write\n"
        + result.stdout
        + result.stderr
    )


# --- mkdocs nav covers MANIFEST and project docs. ---

_NAV_SSOT_RE = re.compile(r"ssot/([\w.-]+\.(?:md|ya?ml))")
_NAV_ACTIVE_RE = re.compile(r"project/(Infra-[\w.-]+\.md)")
_NAV_ARCHIVED_RE = re.compile(r"project/archive/(Infra-[\w.-]+\.md)")
_NAV_ARCHIVE_INDEX_RE = re.compile(
    r"^\s*- Archived Projects: project/archive/README\.md\s*$", re.MULTILINE
)
_SKIP_SUFFIXES = (".TODOWRITE.md", ".SUMMARY.md")


def test_mkdocs_nav_lists_every_manifest_topic() -> None:
    nav_files = set(_NAV_SSOT_RE.findall(MKDOCS.read_text(encoding="utf-8")))
    owners = {e["owner"].split("/")[-1] for e in _manifest()["entries"].values()}
    assert owners
    missing = sorted(owners - nav_files)
    assert not missing, (
        "MANIFEST topics missing from docs/mkdocs.yml nav:\n" + "\n".join(missing)
    )


def test_mkdocs_nav_lists_every_active_project_doc_and_the_archive_index() -> None:
    text = MKDOCS.read_text(encoding="utf-8")
    nav_files = set(_NAV_ACTIVE_RE.findall(text)) - set(_NAV_ARCHIVED_RE.findall(text))
    on_disk = {
        p.name
        for p in PROJECT_DIR.glob("Infra-*.md")
        if not p.name.endswith(_SKIP_SUFFIXES)
    }
    assert on_disk
    assert nav_files
    missing = sorted(on_disk - nav_files)
    assert not missing, "Project docs missing from mkdocs nav:\n" + "\n".join(missing)
    assert ARCHIVE_INDEX.is_file()
    assert _NAV_ARCHIVE_INDEX_RE.search(text), "nav lacks project/archive/README.md"
    stale = sorted(set(_NAV_ARCHIVED_RE.findall(text)))
    assert not stale, "nav lists per-project archive pages:\n" + "\n".join(stale)


# --- SSOT governance: manifest and files agree in both directions. ---

# Files that are not MANIFEST topics.
_ALLOWED_NON_TOPICS = {"README.md", "template.md", "MANIFEST.yaml"}
_README_ROW_RE = re.compile(r"\|\s*\[[^\]]+\]\(\./([^)]+)\)\s*\|\s*`([^`]+)`\s*\|")


def test_every_ssot_doc_is_governed() -> None:
    owned: set[str] = set()
    for entry in _manifest()["entries"].values():
        owned.add(entry["owner"].split("/")[-1])
        for proof in entry.get("proofs", []):
            if proof.startswith("docs/ssot/"):
                owned.add(proof.split("/")[-1])
    files = [
        p
        for p in SSOT_DIR.iterdir()
        if p.is_file() and p.suffix in {".md", ".yaml", ".yml", ".json"}
    ]
    assert files
    ungoverned = sorted(
        p.name for p in files if p.name not in owned | _ALLOWED_NON_TOPICS
    )
    assert not ungoverned, "docs/ssot files without owner or proof:\n" + "\n".join(
        ungoverned
    )


def test_manifest_owners_and_proofs_exist_and_proofs_are_declared() -> None:
    manifest = _manifest()
    assert manifest["version"] == 1
    assert manifest["entries"]
    owners = []
    for key, entry in manifest["entries"].items():
        assert entry["description"], key
        assert (ROOT / _strip_anchor(entry["owner"])).exists(), f"{key}: owner"
        owners.append(entry["owner"])
        assert entry.get("proofs"), f"{key} declares no proof"
        for proof in entry["proofs"]:
            assert (ROOT / _strip_anchor(proof)).exists(), f"{key}: proof {proof}"
    assert len(owners) == len(set(owners))


def test_ssot_readme_index_matches_manifest() -> None:
    entries = _manifest()["entries"]
    readme = {
        key: f"docs/ssot/{name}"
        for name, key in _README_ROW_RE.findall(
            (SSOT_DIR / "README.md").read_text(encoding="utf-8")
        )
    }
    assert set(readme) == set(entries)
    for key, owner in readme.items():
        assert entries[key]["owner"] == owner


def test_ssot_families_are_declared_and_cover_every_entry() -> None:
    manifest = _manifest()
    families = manifest.get("families")
    assert isinstance(families, dict)
    assert 6 <= len(families) <= 8, f"expected 6 to 8 families, got {len(families)}"
    for name, family in families.items():
        boundary = family.get("boundary") if isinstance(family, dict) else None
        assert isinstance(boundary, str) and boundary.strip(), f"{name}: no boundary"
    used = set()
    for key in manifest["entries"]:
        family = key.split(".", 1)[0]
        assert family in families, f"{key}: undeclared family {family}"
        used.add(family)
    assert not set(families) - used, f"empty families: {sorted(set(families) - used)}"


# --- Project docs: links and proof paths resolve. ---

_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PROOF_PREFIXES = (
    ".github/",
    "bootstrap/",
    "cloudflare/",
    "docs/ssot/",
    "common/",
    "platform/",
    "scripts/",
    "tools/",
)
# Floors for the archive index (#1826): 29 AC rows and 60 distinct proof paths.
ARCHIVE_INDEX_AC_ROWS = 29
ARCHIVE_INDEX_MIN_PROOF_PATHS = 60
_AC_ROW_RE = re.compile(r"^\| Infra-0\d+\.\d+ \|", re.MULTILINE)


def _missing_proof_paths(label: str, lines: list[str]) -> tuple[list[str], set[str]]:
    missing: list[str] = []
    checked: set[str] = set()
    for line in lines:
        for target in _BACKTICK_RE.findall(line):
            if not target.startswith(_PROOF_PREFIXES) or " " in target:
                continue
            if "*" in target and list(ROOT.glob(_strip_anchor(target))):
                continue
            checked.add(target)
            if not (ROOT / _strip_anchor(target)).exists():
                missing.append(f"{label} -> {target}")
    return missing, checked


def test_project_ssot_links_target_existing_files() -> None:
    docs = sorted(PROJECT_DIR.glob("*.md"))
    assert docs
    missing: list[str] = []
    for path in docs:
        for target in _MD_LINK_RE.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if "../ssot/" not in target:
                continue
            resolved = (path.parent / _strip_anchor(target)).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")
    assert missing == []


def test_archive_index_keeps_every_ac_row_and_proof_path() -> None:
    text = ARCHIVE_INDEX.read_text(encoding="utf-8")
    missing, checked = _missing_proof_paths(
        str(ARCHIVE_INDEX.relative_to(ROOT)), [text]
    )
    rows = len(_AC_ROW_RE.findall(text))
    assert rows == ARCHIVE_INDEX_AC_ROWS, f"index has {rows} AC rows"
    assert len(checked) >= ARCHIVE_INDEX_MIN_PROOF_PATHS, f"checked {len(checked)}"
    assert missing == []


# --- Code and docs reference each other: references must resolve. ---

# tools/doc_link_check.py records unresolved paths on purpose; scanning it would fail on them.
_RECORDS_UNRESOLVED_PATHS = {"doc_link_check.py"}
_DOC_PATH_RE = re.compile(r"docs/(?:ssot|project)/[\w./-]+\.(?:md|ya?ml)")
_AC_RE = re.compile(r"Infra-0\d+\.\d+")
_PROOF_PATH_RE = re.compile(r"`([\w./-]+\.(?:py|ya?ml|js|json|sh|hcl|md|ctmpl))`")
_INFRA2_PREFIX_RE = re.compile(
    r"^(?:common|tools|platform|bootstrap|docs|cloudflare|finance_report|scripts|\.github)/"
)
_SDK_DOTTED_RE = re.compile(r"\binfra2[_-]sdk(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_DOTTED_REPO_REF_RE = re.compile(r"`((?:common|tools)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")
_TEST_FILE_REF_RE = re.compile(r"`(common/tests/test_[A-Za-z0-9_]+\.py)`")
_FILE_SUFFIXES = (".py", ".md", ".yaml", ".yml", ".json", ".sh", ".hcl", ".ctmpl")


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


def _epic_docs() -> list[Path]:
    docs = sorted(PROJECT_DIR.glob("Infra-*.md"))
    assert ARCHIVE_INDEX.is_file()
    return [*docs, ARCHIVE_INDEX]


def _active_governed_docs() -> list[Path]:
    names = ("init.md", "README.md", "SECRETS.md")
    docs = [p for p in (ROOT / n for n in names) if p.is_file()]
    for sub in ("docs", "common", "tools", "platform", "bootstrap", "harness"):
        base = ROOT / sub
        if base.is_dir():
            for ext in ("*.md", "*.yaml", "*.yml"):
                docs += [p for p in base.rglob(ext) if "archive" not in p.parts]
    return sorted(set(docs))


def _resolve_dotted(ref: str, sdk: bool) -> str | None:
    """Return an error text when the dotted reference does not import, else None."""
    if sdk and ref.startswith("infra2-sdk."):
        return f"hyphenated package prefix in {ref!r}"
    if not sdk and ref.endswith(_FILE_SUFFIXES):
        return None
    parts = ref.split(".")
    for i in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:i])
        try:
            obj: object = importlib.import_module(mod_name)
            for attr in parts[i:]:
                if not hasattr(obj, attr):
                    return f"{ref!r}: {mod_name!r} has no attribute {attr!r}"
                obj = getattr(obj, attr)
            return None
        except Exception:
            continue
    return f"{ref!r}: cannot resolve"


def _block_import_errors(text: str, label: str) -> list[str]:
    errors: list[str] = []
    for m in re.finditer(r"```python\n(.*?)```", text, re.S):
        try:
            tree = ast.parse(m.group(1))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module):
                continue
            if not node.module.startswith(("common", "tools", "infra2_sdk")):
                continue
            try:
                mod = importlib.import_module(node.module)
            except Exception as exc:
                errors.append(f"{label}: cannot import {node.module!r} ({exc})")
                continue
            errors += [
                f"{label}: {node.module!r} has no attribute {a.name!r}"
                for a in node.names
                if a.name != "*" and not hasattr(mod, a.name)
            ]
    return errors


def _module_names(py_file: Path) -> set[str]:
    defined: set[str] = set()
    for node in ast.parse(py_file.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            defined.update(a.asname or a.name for a in node.names)
    return defined


def _module_map_errors(readme: Path) -> list[str]:
    label = str(readme.relative_to(ROOT))
    errors: list[str] = []
    for line in readme.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 3 or cells[0] in ("Module", "--------", "---"):
            continue
        mod = re.search(r"`([A-Za-z0-9_./]+\.py)`", cells[0])
        if not mod:
            continue
        py_file = readme.parent / mod.group(1)
        if not py_file.is_file():
            errors.append(f"{label}: missing module {mod.group(1)}")
            continue
        defined = _module_names(py_file)
        for tok in re.findall(r"`([^`]+)`", cells[2]):
            name = tok.removesuffix("()").split(".")[0]
            if re.fullmatch(r"[A-Za-z_]\w*", name) and name not in defined:
                errors.append(f"{label} ({mod.group(1)}): symbol {name!r} not in AST")
    return errors


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def test_code_doc_path_references_resolve() -> None:
    files = _code_py_files()
    assert files
    missing = [
        f"{p.relative_to(ROOT)} -> {ref}"
        for p in files
        for ref in set(_DOC_PATH_RE.findall(_read(p)))
        if not (ROOT / ref).exists()
    ]
    assert not missing, "doc paths in code do not resolve:\n" + "\n".join(
        sorted(missing)
    )


def test_ac_ids_cited_in_tests_are_defined_in_an_epic() -> None:
    defined: set[str] = set()
    for path in _epic_docs():
        defined |= set(_AC_RE.findall(_read(path)))
    assert defined
    orphans: dict[str, list[str]] = {}
    for path in (ROOT / "common/tests").rglob("test_*.py"):
        if path.name == THIS:
            continue
        for ac in set(_AC_RE.findall(_read(path))):
            if ac not in defined:
                orphans.setdefault(ac, []).append(path.name)
    assert not orphans, f"tests cite AC ids no EPIC defines: {orphans}"


def test_ac_proof_file_paths_in_epics_exist() -> None:
    missing: list[str] = []
    rows = 0
    for doc in _epic_docs():
        for line in _read(doc).splitlines():
            if not _AC_RE.search(line):
                continue
            rows += 1
            for ref in _PROOF_PATH_RE.findall(line):
                if "*" in ref or not _INFRA2_PREFIX_RE.match(ref):
                    continue
                if not (ROOT / ref).exists():
                    missing.append(f"{doc.name}: {ref}")
    assert rows
    assert not missing, "EPIC AC proof paths do not resolve:\n" + "\n".join(
        sorted(missing)
    )


def test_active_docs_infra2_sdk_references_resolve() -> None:
    errors: list[str] = []
    checked = 0
    for doc in _active_governed_docs():
        for ref in sorted(set(_SDK_DOTTED_RE.findall(_read(doc)))):
            checked += 1
            err = _resolve_dotted(ref, sdk=True)
            if err:
                errors.append(f"{doc.relative_to(ROOT)}: {err}")
    assert checked >= 15, f"expected >= 15 infra2_sdk references, got {checked}"
    assert not errors, "unresolved infra2_sdk references:\n" + "\n".join(errors)


def test_common_domain_readme_module_maps_match_ast_exports() -> None:
    readmes = sorted((ROOT / "common").glob("*/README.md"))
    assert len(readmes) >= 5
    errors = [e for r in readmes for e in _module_map_errors(r)]
    assert not errors, "Module Map drift:\n" + "\n".join(errors)


def test_active_docs_imports_dotted_refs_and_test_paths_resolve() -> None:
    errors: list[str] = []
    seen_md = 0
    for doc in _active_governed_docs():
        if doc.suffix != ".md":
            continue
        seen_md += 1
        rel = str(doc.relative_to(ROOT))
        text = _read(doc)
        errors += _block_import_errors(text, rel)
        for m in _DOTTED_REPO_REF_RE.finditer(text):
            err = _resolve_dotted(m.group(1), sdk=False)
            if err:
                errors.append(f"{rel}: {err}")
        errors += [
            f"{rel}: missing test file {m.group(1)!r}"
            for m in _TEST_FILE_REF_RE.finditer(text)
            if not (ROOT / m.group(1)).is_file()
        ]
    assert seen_md
    assert not errors, "doc import/symbol/test-path drift:\n" + "\n".join(errors)


def test_delivery_model_has_one_owner() -> None:
    owner = SSOT_DIR / "ops.pipeline.md"
    assert "### 2.0 Delivery model" in owner.read_text(encoding="utf-8")
    stage_model = re.compile(
        r"^\s*[-*]?\s*\*{0,2}Stage [123]\b.{0,4}(?:—|--|-|:)|三阶段|三段式|交付阶段.{0,6}SSOT|阶段定义|Pre\s*(?:->|→)\s*Deploy",
        re.M,
    )
    scanned = [
        ROOT / "init.md",
        ROOT / "README.md",
        *sorted(SSOT_DIR.glob("*.md")),
        *sorted((ROOT / "docs/onboarding").glob("*.md")),
    ]
    copies = [
        str(p.relative_to(ROOT))
        for p in scanned
        if p != owner and stage_model.search(p.read_text(encoding="utf-8"))
    ]
    assert not copies, f"Stage model copied outside ops.pipeline.md 2.0: {copies}"


def test_docs_claim_no_unrun_canary_verify_soak() -> None:
    """#1635: no workflow runs `canary_verify` or a 10 minute soak, so docs must not claim it."""
    pattern = re.compile(
        r"canary_verify --env staging|10 分钟浸润|runs Canary verification"
    )
    files = [ROOT / "init.md", *sorted((ROOT / "docs").rglob("*.md"))]
    assert len(files) > 1
    hits = [
        f"{f.relative_to(ROOT)}:{n}"
        for f in files
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []


# --- AGENTS.md is the one committed constitution; CLAUDE.md stays absent. ---

AGENTS = "AGENTS.md"
# A slash is required so that a bare filename used as shorthand is not read as a path.
_BACKTICKED_PATH_RE = re.compile(
    r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:py|sh|yaml|yml|md|json))`"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True
    )


def test_agents_md_is_committed_as_a_regular_file() -> None:
    assert _git("cat-file", "-e", f"HEAD:{AGENTS}").returncode == 0, (
        "AGENTS.md not in HEAD"
    )
    out = _git("ls-files", "-s", AGENTS).stdout.split()
    assert out and out[0] == "100644", f"AGENTS.md mode is {out[:1]}, not 100644"
    path = ROOT / AGENTS
    assert path.is_file() and not path.is_symlink()
    assert path.read_text(encoding="utf-8").strip(), "AGENTS.md is empty"


def test_claude_md_is_absent_so_native_agents_md_loads() -> None:
    """Any CLAUDE.md in the directory walk suppresses native AGENTS.md loading."""
    assert _git("ls-files", "CLAUDE.md").stdout.strip() == ""
    assert _git("cat-file", "-e", "HEAD:CLAUDE.md").returncode != 0
    claude = ROOT / "CLAUDE.md"
    assert not claude.exists() and not claude.is_symlink()


def test_agents_md_paths_and_the_provability_pointer_resolve() -> None:
    agents = (ROOT / AGENTS).read_text(encoding="utf-8")
    paths = set(_BACKTICKED_PATH_RE.findall(agents))
    assert paths
    missing = sorted(p for p in paths if not (ROOT / p).exists())
    assert not missing, "AGENTS.md names missing paths: " + ", ".join(missing)

    import common.gate.pr_merge_gate as gate

    name = "_direction_proof_for"
    assert hasattr(gate, name), f"AGENTS.md section 6 points at missing {name}()"
    assert f"`{name}()`" in agents, f"AGENTS.md no longer names `{name}()`"
    decide = getattr(gate, name)
    assert decide("docs/ssot/ci-gate-inventory.yaml") is not None
    assert decide(".github/workflows/infra-ci.yml") is not None
    assert decide("tools/pr_merge_gate.py") is None, "gate Python must stay unprovable"


# --- .python-version is the one source of the runtime Python version (#1506). ---

VERSION_FILE = ROOT / ".python-version"
# Jobs that AGENTS.md makes production paths keep an explicit pin and never read the file.
PRODUCTION_JOBS = {
    (".github/workflows/ops-checks.yml", job) for job in UNCLASSIFIED_SCHEDULED_JOBS
}
# Images that a merge can redeploy to production at once; their move needs the owner.
PENDING_OWNER: dict[str, str] = {}
_IMAGE_REF = re.compile(r"(?<![\w./-])(?:docker\.io/)?(?:library/)?python:([\w.@:-]+)")
_SCANNED_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".toml")
_SKIPPED_PREFIXES = (
    "repos/",
    "oh-my-code-agent/",
    "common/tests/",
    "docs/project/archive/",
)


def _version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _tracked(*patterns: str) -> list[str]:
    return subprocess.run(
        ["git", "ls-files", "--", *patterns],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def _workflow_steps():
    for rel in _tracked(
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
        ".github/workflows/**/*.yml",
        ".github/workflows/**/*.yaml",
    ):
        doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                yield rel, job_id, step


def test_python_version_file_and_workflow_pins_agree() -> None:
    assert re.fullmatch(r"3\.\d+", _version()), _version()
    steps = list(_workflow_steps())
    seen = [
        ((rel, job), step.get("with") or {})
        for rel, job, step in steps
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert len(seen) >= 10, "too few setup-python steps"
    assert PRODUCTION_JOBS <= {key for key, _ in seen}, "production job lost setup"
    wrong = []
    for key, inputs in seen:
        if key in PRODUCTION_JOBS:
            if not re.fullmatch(r"3\.\d+", str(inputs.get("python-version", ""))) or (
                "python-version-file" in inputs
            ):
                wrong.append((key, inputs))
        elif (
            "python-version" in inputs
            or inputs.get("python-version-file") != ".python-version"
        ):
            wrong.append((key, inputs))
    assert not wrong, f"setup-python steps break the version rule: {wrong}"
    pinned = [
        (rel, job)
        for rel, job, step in steps
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv")
        and "python-version" in (step.get("with") or {})
    ]
    assert not pinned, f"setup-uv steps pin their own Python: {pinned}"


def test_every_python_image_reference_has_the_version_of_the_file() -> None:
    version = _version()
    refs = []
    for rel in _tracked("*"):
        name = rel.rsplit("/", 1)[-1]
        if rel.startswith(_SKIPPED_PREFIXES):
            continue
        if not ("Dockerfile" in name or rel.endswith(_SCANNED_SUFFIXES)):
            continue
        path = ROOT / rel
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            refs += [(rel, tag) for tag in _IMAGE_REF.findall(text)]
    assert len({rel for rel, _ in refs if "Dockerfile" in rel}) >= 3, refs
    pin = re.compile(rf"{re.escape(version)}-slim@sha256:[0-9a-f]{{64}}")
    wrong = [
        (rel, tag)
        for rel, tag in refs
        if not pin.fullmatch(tag) and f"python:{tag}" != PENDING_OWNER.get(rel)
    ]
    assert not wrong, f"python images not on {version}-slim pinned by digest: {wrong}"
    for rel, image in PENDING_OWNER.items():
        assert f"FROM {image}" in (ROOT / rel).read_text(encoding="utf-8"), (
            f"{rel} no longer uses {image}: remove the entry"
        )
