"""The index promises newest first; sorting paths as text only agreed by accident.

`_project_docs` sorted `Path` objects, which compares them as strings. That
matches numeric order exactly while every Infra id is the same width, and
stops the moment one is not: "Infra-999" sorts above "Infra-1000" as text, so
the newest document would appear below an older one, under a comment
promising the opposite and with nothing red.

Dormant — the ids are in the twenties — and silent when it arrives, which is
why it is worth a test rather than a note.

**Every assertion goes through `_project_docs`.** The first version of this
file called `_project_number` directly and passed just as happily with the
ordering reverted, because it was measuring the helper rather than the
function whose behaviour changed. Reverse-verification caught that; reading it
had not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import gen_project_index


def _docs(tmp_path: Path, *names: str) -> list[str]:
    """Real files in a real directory, ordered by the real function."""
    for name in names:
        (tmp_path / name).write_text(
            "# x\n\n**Status**: In Progress\n", encoding="utf-8"
        )
    return [p.name.split(".")[0] for p in gen_project_index._project_docs(tmp_path)]


def test_a_four_digit_id_sorts_above_a_three_digit_one(tmp_path: Path) -> None:
    """The case text sorting gets wrong, asked of `_project_docs` itself."""
    assert _docs(tmp_path, "Infra-999.a.md", "Infra-1000.b.md") == [
        "Infra-1000",
        "Infra-999",
    ]


def test_text_sorting_would_disagree() -> None:
    """The control. Without it the assertion above could hold under the old
    implementation too, and prove nothing about the change."""
    assert sorted(["Infra-999.a.md", "Infra-1000.b.md"], reverse=True)[0].startswith(
        "Infra-999"
    ), "text sorting no longer disagrees, so the test above stopped discriminating"


def test_same_width_ids_keep_their_order(tmp_path: Path) -> None:
    """Today's ids are all three digits, so the committed README must not move."""
    assert _docs(tmp_path, "Infra-006.a.md", "Infra-021.b.md", "Infra-013.c.md") == [
        "Infra-021",
        "Infra-013",
        "Infra-006",
    ]


def test_todowrite_and_summary_are_still_skipped(tmp_path: Path) -> None:
    """The ordering change must not quietly widen what counts as a project doc."""
    assert _docs(
        tmp_path, "Infra-007.a.md", "Infra-008.TODOWRITE.md", "Infra-009.SUMMARY.md"
    ) == ["Infra-007"]


def test_a_name_without_an_id_is_refused_rather_than_sorted_last() -> None:
    """Returning a sentinel would bury the file at one end and call that an answer."""
    with pytest.raises(AssertionError):
        gen_project_index._project_number(Path("README.md"))
