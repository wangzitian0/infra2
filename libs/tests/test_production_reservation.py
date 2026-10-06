"""The owner's one reservation is pinned here (owner instruction, 2026-10-06).

Only a production deployment needs the owner's approval and presence. Every other
stage is the agent's behind its checklist, including changes to the merge gate. The
reservation must not be weakened through that same checklist, so:

* this file is in ``PRODUCTION_GUARD_FILES``: a change to it needs the owner unless
  a mechanical tightening proof covers it;
* it pins the text that defines the reservation, so narrowing that text fails here
  and forces a change to this file;
* it pins which files guard production, so the guard set cannot shrink quietly.
"""

from __future__ import annotations

from pathlib import Path

from libs.gate.types import PRODUCTION_GUARD_FILES

ROOT = Path(__file__).resolve().parents[2]

# Sentences that define the reservation. Change them only with the owner's approval.
RESERVATION_TEXT = {
    "docs/ssot/ops.merge-gate.md": (
        "**Only one class needs the owner: a production deployment**",
        "Production today means: prod apply, prod promote, L1 bootstrap self-update, "
        "a runner rebuild triggered by `bootstrap/06.iac_runner/**`, and the "
        "observability apply that writes live SigNoz.",
    ),
}

EXPECTED_GUARD_FILES = frozenset(
    {
        "tools/pr_merge_gate.py",
        "libs/gate/__init__.py",
        "libs/gate/client.py",
        "libs/gate/evaluator.py",
        "libs/gate/inventory.py",
        "libs/gate/self_governance.py",
        "libs/gate/types.py",
        "libs/tests/test_production_reservation.py",
    }
)


def test_the_reservation_text_is_present_verbatim() -> None:
    for rel, sentences in RESERVATION_TEXT.items():
        text = " ".join((ROOT / rel).read_text(encoding="utf-8").split())
        for sentence in sentences:
            assert " ".join(sentence.split()) in text, (
                f"{rel} no longer states the production reservation: {sentence!r}. "
                "Narrowing it needs the owner's approval of the head SHA."
            )


def test_the_production_guard_set_does_not_shrink() -> None:
    assert PRODUCTION_GUARD_FILES >= EXPECTED_GUARD_FILES, sorted(
        EXPECTED_GUARD_FILES - PRODUCTION_GUARD_FILES
    )


def test_every_guard_file_exists() -> None:
    missing = sorted(f for f in PRODUCTION_GUARD_FILES if not (ROOT / f).is_file())
    assert not missing, missing


def test_this_file_guards_itself() -> None:
    assert "libs/tests/test_production_reservation.py" in PRODUCTION_GUARD_FILES
