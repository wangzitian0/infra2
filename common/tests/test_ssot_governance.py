"""SSOT manifest and project-link governance checks."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SSOT_DIR = ROOT / "docs/ssot"
PROJECT_DIR = ROOT / "docs/project"
MANIFEST = SSOT_DIR / "MANIFEST.yaml"
README = SSOT_DIR / "README.md"


README_ROW_RE = re.compile(r"\|\s*\[[^\]]+\]\(\./([^)]+)\)\s*\|\s*`([^`]+)`\s*\|")
MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
BACKTICK_PATH_RE = re.compile(r"`([^`]+)`")
PROOF_PATH_PREFIXES = (
    ".github/",
    "bootstrap/",
    "cloudflare/",
    "docs/ssot/",
    "common/",
    "platform/",
    "scripts/",
    "tools/",
)


def _strip_anchor(path: str) -> str:
    return path.split("#", 1)[0].split("::", 1)[0]


def _load_manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _readme_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for file_name, key in README_ROW_RE.findall(README.read_text(encoding="utf-8")):
        entries[key] = f"docs/ssot/{file_name}"
    return entries


def test_ssot_manifest_owners_and_proofs_exist() -> None:
    """SSOT owner/proof paths must be lintable and present in the repo."""
    manifest = _load_manifest()

    assert manifest["version"] == 1
    assert manifest["entries"]

    owners: list[str] = []
    for key, entry in manifest["entries"].items():
        owner = entry["owner"]
        owner_path = ROOT / _strip_anchor(owner)
        owners.append(owner)

        assert key
        assert entry["description"]
        assert owner_path.exists(), f"{key}: missing owner {owner}"

        for proof in entry.get("proofs", []):
            assert (ROOT / _strip_anchor(proof)).exists(), (
                f"{key}: missing proof {proof}"
            )

    assert len(owners) == len(set(owners))


def test_ssot_readme_index_matches_manifest() -> None:
    """Human SSOT index and machine manifest must describe the same keys."""
    manifest_entries = _load_manifest()["entries"]
    readme_entries = _readme_entries()

    # README.md is navigation, not an owned topic row. Every other SSOT topic
    # should appear in both places with the same owner path.
    assert set(readme_entries) == set(manifest_entries)

    for key, readme_owner in readme_entries.items():
        assert manifest_entries[key]["owner"] == readme_owner


def test_project_ssot_links_target_existing_files() -> None:
    """Project docs may reference SSOT freely, but links must not rot."""
    missing: list[str] = []

    for path in sorted(PROJECT_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for target in MD_LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if "../ssot/" not in target:
                continue

            target_path = (path.parent / _strip_anchor(target)).resolve()
            try:
                target_path.relative_to(ROOT)
            except ValueError:
                missing.append(f"{path.relative_to(ROOT)} -> {target}")
                continue
            if not target_path.exists():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")

    assert missing == []


def test_project_ac_proof_paths_target_existing_files() -> None:
    """Project AC proof table paths must not point at absent anchors."""
    missing: list[str] = []

    for path in sorted(PROJECT_DIR.glob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "| Infra-" not in line:
                continue
            for target in BACKTICK_PATH_RE.findall(line):
                if not target.startswith(PROOF_PATH_PREFIXES):
                    continue
                if " " in target or "\n" in target:
                    continue

                matches = (
                    list(ROOT.glob(_strip_anchor(target))) if "*" in target else []
                )
                if matches:
                    continue
                if not (ROOT / _strip_anchor(target)).exists():
                    missing.append(f"{path.relative_to(ROOT)} -> {target}")

    assert missing == []


def test_redesigned_pipeline_validation_command_targets_existing_files() -> None:
    """Infra-011 validation commands must stay runnable as proof files move."""
    missing: list[str] = []
    path = PROJECT_DIR / "archive" / "Infra-011.reliability_hardening.md"

    for target in BACKTICK_PATH_RE.findall(path.read_text(encoding="utf-8")):
        if not target.startswith(PROOF_PATH_PREFIXES):
            continue
        if " " in target or "\n" in target:
            continue
        matches = list(ROOT.glob(_strip_anchor(target))) if "*" in target else []
        if matches:
            continue
        if not (ROOT / _strip_anchor(target)).exists():
            missing.append(f"{path.relative_to(ROOT)} -> {target}")

    for line in path.read_text(encoding="utf-8").splitlines():
        target = line.strip().rstrip("\\")
        if not target.startswith(PROOF_PATH_PREFIXES):
            continue
        if " " in target or "\n" in target:
            continue
        if not (ROOT / _strip_anchor(target)).exists():
            missing.append(f"{path.relative_to(ROOT)} -> {target}")

    assert missing == []


def test_code_owned_ssot_entries_have_test_or_code_proofs() -> None:
    """Every SSOT topic entry must carry at least one verifiable code or test proof anchor."""
    manifest_entries = _load_manifest()["entries"]

    for key, entry in manifest_entries.items():
        assert entry.get("proofs"), (
            f"{key} must declare at least one proof anchor in MANIFEST.yaml"
        )


def test_ssot_families_are_declared_and_cover_every_entry() -> None:
    """Each manifest key maps to one declared family. Six to eight families, none empty."""
    manifest = _load_manifest()
    families = manifest.get("families")
    assert isinstance(families, dict), "MANIFEST.yaml must declare a families mapping"

    assert 6 <= len(families) <= 8, f"expected 6 to 8 families, got {len(families)}"
    for name, family in families.items():
        boundary = family.get("boundary") if isinstance(family, dict) else None
        assert isinstance(boundary, str) and boundary.strip(), (
            f"family {name} has no boundary"
        )

    used: set[str] = set()
    for key in manifest["entries"]:
        family = key.split(".", 1)[0]
        assert family in families, f"entry {key} has undeclared family {family}"
        used.add(family)

    empty = set(families) - used
    assert not empty, f"declared families with no entry: {sorted(empty)}"
