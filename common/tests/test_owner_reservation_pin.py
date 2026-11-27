"""Pins the sentences of the owner's production reservation in infra2 sources."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PINS: dict[str, tuple[str, ...]] = {
    "AGENTS.md": (
        "## Production reservation",
        "The single definition of the production reservation.",
        "**Only a production change returns to the owner.** A production change alters what production runs or stores (deploy, promote or apply to production; production secret write; manual production data write or deletion; production DNS, route or host change; unrecallable release; or scheduled unapproved production code).",
        "Each Repo tier names its production paths.",
        "A path can be a merge or a release-checkout pull.",
        "**Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs.",
        'A "deploy" reply covers only the release that it names.',
        "Immediately before a production dispatch, get a live reply from the owner.",
        "**Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA.",
        "This includes an edit that makes fewer production runs wait for the owner (deploy-path approval check, production job classification, or production environment settings).",
        "It also covers removing or weakening a pin of this section, its pin test, or the required check that runs the test.",
        "A looser edit in the self-guard follows the self-guard.",
        "**Every other hold is a defect:**",
        "**Production reservation:** the Root section of that name is the only definition of what the owner decides.",
        "This repository also names as production: L1 bootstrap self-update, a runner rebuild (the `bootstrap` job of `deploy.yml`, whatever its `type` input), and the observability apply.",
        "A production path of rule 6 stops the merge whatever the exit code.",
        "These edits are production changes in this repository, whatever the exit code.",
        "First, an edit to the settings of the `production` environment.",
        "Second, an edit to the jobs `host-hygiene-schedule` or `facet-reconcile` of `ops-checks.yml`, or to `tools/host_hygiene_schedule.py` or `tools/facet_reconcile.py`.",
        "Third, an edit to a workflow or to the release code when the verdict does not print `production lock verified`.",
        "Without the lock, such an edit can reach production with no reviewer.",
        "An edit that makes the contract or the lock facts gate fewer production jobs follows the Root self-guard.",
    ),
    ".agents/skills/deploy-evidence/SKILL.md": (
        "Immediately before dispatch, get a live reply from the owner in this session; an earlier or asynchronous answer is not enough.",
    ),
    ".agents/skills/release-train/SKILL.md": (
        "Never dispatch production without the owner's live reply in the session (Root section `Production reservation`).",
    ),
}

_LINE_START = re.compile(r"(?:\A|\n)[ \t]*(?:(?:[->+]|\d+\.)[ \t]+)*\Z")
_AFTER_SENTENCE_END = re.compile(r"\.\s+\Z")


def _pattern(sentence: str) -> re.Pattern[str]:
    return re.compile(r"\s+".join(re.escape(word) for word in sentence.split()))


def starts_a_sentence(text: str, sentence: str) -> bool:
    """True when `sentence` occurs in `text` at the start of a sentence."""
    for match in _pattern(sentence).finditer(text):
        before = text[: match.start()]
        if _LINE_START.search(before) or _AFTER_SENTENCE_END.search(before):
            return True
    return False


def test_reservation_sentences_are_present() -> None:
    for rel, sentences in PINS.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for sentence in sentences:
            assert _pattern(sentence).search(text), (
                f"pinned sentence left {rel}: {sentence[:60]!r}"
            )
            assert starts_a_sentence(text, sentence), (
                f"pinned sentence has a lead-in in {rel}: {sentence[:60]!r}"
            )


def test_the_sentence_start_check_rejects_lead_ins() -> None:
    pin = "A release that cannot be recalled is production."
    for accepted in (
        pin,
        f"Owner text. {pin}",
        f"Owner text.\n{pin}",
        f"- {pin}",
        f"> {pin}",
        f"5. {pin}",
    ):
        assert starts_a_sentence(accepted, pin)
    for rejected in (
        f"Owner text. Not {pin}",
        f"It is false that {pin}",
        f"**Not so:** {pin}",
        f"Unless the owner objects; {pin}",
        f"- Not {pin}",
    ):
        assert not starts_a_sentence(rejected, pin)
