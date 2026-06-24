"""Doc-consistency guards: fail locally (in seconds) the instant a referenced doc/AC drifts,
instead of at CI-runtime in whatever test happens to read it.

Two drift surfaces these close:
1. Code -> doc PATH references (test_*.py reading `docs/.../x.md`): a moved/deleted doc must
   fail here, not only when that specific content-proof test runs.
2. docs -> AC -> test traceability (`Infra-XX.Y`): a test citing an AC id that no EPIC defines
   (typo / renamed / deleted EPIC) is a silent orphan today — there is no registry guard.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).name

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
    r"^(?:libs|tools|platform|bootstrap|docs|cloudflare|finance_report|scripts|\.github)/"
)


def _code_py_files() -> list[Path]:
    files: list[Path] = []
    for base in ("libs", "tools", "platform", "bootstrap"):
        files.extend((ROOT / base).rglob("*.py"))
    return [f for f in files if f.name != THIS and "__pycache__" not in f.parts]


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
        defined |= set(_AC_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return defined


def test_ac_ids_cited_in_tests_are_defined_in_an_epic() -> None:
    """Every `Infra-XX.Y` AC id cited in a test must be DEFINED in some EPIC doc — so a typo or
    a citation to a renamed/removed AC fails loudly instead of orphaning silently."""
    defined = _defined_ac_ids()
    assert defined, "no AC ids found in docs/project/Infra-*.md (parser drift?)"

    orphans: dict[str, list[str]] = {}
    for path in (ROOT / "libs/tests").rglob("test_*.py"):
        if path.name == THIS:
            continue
        for ac in set(_AC_RE.findall(path.read_text(encoding="utf-8", errors="ignore"))):
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
    assert not missing, "EPIC AC proof paths do not resolve:\n" + "\n".join(sorted(missing))
