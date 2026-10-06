"""Guard the credential reach record (docs/ssot/ops.credential-reach.md, #675).

The record states what each credential can reach. It decays when a workflow gains a
secret or a service gains an AppRole and nobody adds a row. These checks fail in that
case. They also fail when a row loses its evidence or when the file holds a string that
looks like a credential value. The record holds names and scopes only.
"""

from __future__ import annotations

import re
from pathlib import Path

from libs.security.registry import SERVICES

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/ssot/ops.credential-reach.md"
WORKFLOWS = ROOT / ".github/workflows"
OBSERVABILITY = ROOT / "docs/ssot/ops.observability.md"

SECRET_REF = re.compile(r"secrets\.([A-Za-z0-9_]+)")
CREDENTIAL_ROW = re.compile(r"^\|\s*C(\d{2})\s*\|")
UNVERIFIED = "unverified: needs console check"

# Heuristic shapes of credential values. Each pattern needs a long unbroken run, so a
# file path, a variable name, or a sentence does not match.
SECRET_SHAPES = {
    "provider prefix": re.compile(
        r"\b(?:ghp|gho|ghs|ghu|github_pat|ops|sk|xox[abp])[_-][A-Za-z0-9_-]{16,}"
        r"|\bhvs\.[A-Za-z0-9_-]{16,}"
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "hex run": re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    "mixed-case alphanumeric run": re.compile(
        r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[a-z])"
        r"(?=[A-Za-z0-9]*[A-Z])[A-Za-z0-9]{32,}\b"
    ),
}


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _table_lines() -> list[str]:
    return [line for line in _doc().splitlines() if line.startswith("|")]


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def secret_like(text: str) -> list[str]:
    """Names of the credential shapes found in ``text``."""
    return [name for name, pattern in SECRET_SHAPES.items() if pattern.search(text)]


def test_every_workflow_secret_has_a_backticked_table_row() -> None:
    names = {
        name
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        for name in SECRET_REF.findall(workflow.read_text(encoding="utf-8"))
    }
    assert len(names) >= 20, f"workflow scan found too few secrets: {sorted(names)}"
    lines = _table_lines()
    missing = sorted(
        name for name in names if not any(f"`{name}`" in line for line in lines)
    )
    assert not missing, f"secrets.* names with no table row in {DOC.name}: {missing}"


def test_every_registered_service_has_an_approle_row() -> None:
    assert len(SERVICES) >= 15, "service registry is unexpectedly small"
    lines = _table_lines()
    missing = []
    for service in SERVICES:
        label = f"`{service.id}`" + (" (preview)" if service.preview else "")
        if not any(line.startswith(f"| {label} |") for line in lines):
            missing.append(label)
    assert not missing, f"registered services with no AppRole row: {missing}"


def test_every_credential_row_has_evidence_and_a_rotation_value() -> None:
    rows = [line for line in _table_lines() if CREDENTIAL_ROW.match(line)]
    assert len(rows) >= 40, f"credential row count is too low: {len(rows)}"
    ids = [CREDENTIAL_ROW.match(line).group(1) for line in rows]
    assert len(ids) == len(set(ids)), "duplicate credential row ids"
    problems = []
    for line in rows:
        cells = _cells(line)
        row_id = cells[0]
        if len(cells) != 8:
            problems.append(f"{row_id}: {len(cells)} cells, expected 8")
            continue
        rotation, verified = cells[6], cells[7]
        if not (
            rotation.startswith("no procedure yet")
            or rotation.startswith("not applicable")
            or re.match(r"P\d\b", rotation)
        ):
            problems.append(f"{row_id}: rotation cell has no procedure id")
        if not (UNVERIFIED in verified or verified.startswith("repo:")):
            problems.append(f"{row_id}: verified cell has no evidence")
    assert not problems, "\n".join(problems)


def test_rotation_procedure_ids_resolve_to_section_6() -> None:
    text = _doc()
    section = text.split("## 6. Rotation procedures that exist", 1)[1].split(
        "## 7.", 1
    )[0]
    defined = set(re.findall(r"^\| (P\d) \|", section, flags=re.MULTILINE))
    used = {
        match
        for line in _table_lines()
        if CREDENTIAL_ROW.match(line)
        for match in re.findall(r"\bP\d\b", _cells(line)[6])
    }
    assert defined, "section 6 defines no procedure"
    assert used <= defined, f"rotation ids with no definition: {sorted(used - defined)}"


def test_doc_holds_no_credential_like_string() -> None:
    assert secret_like(_doc()) == []


def test_secret_shape_scan_detects_credential_shapes() -> None:
    """The scan must fail on a credential shape and pass on the prose of this record."""
    fake_values = [
        "ghp_" + "a1B2" * 9,
        "eyJ" + "hbGciOiJIUzI1NiJ9" + "x" * 8,
        "AKIA" + "ABCDEFGH12345678",
        "0123456789abcdef" * 2,
        "Ab1" * 12,
        "hvs." + "AbCdEf1234" * 3,
    ]
    for value in fake_values:
        assert secret_like(f"token {value} end"), (
            f"scan missed a credential shape: {value[:6]}"
        )
    assert (
        secret_like(
            "`bootstrap/05.vault/tasks.py` and `INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL`"
        )
        == []
    )


def test_observability_monthly_audit_line_points_at_the_record() -> None:
    monthly = [
        line
        for line in OBSERVABILITY.read_text(encoding="utf-8").splitlines()
        if "月级" in line
    ]
    assert monthly, "ops.observability.md has no monthly tier row"
    assert any("ops.credential-reach.md" in line for line in monthly)
