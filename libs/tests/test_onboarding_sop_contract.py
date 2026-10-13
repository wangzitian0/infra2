"""Tests ensuring onboarding SOP commands and parameters conform to infra2_sdk contracts."""

from __future__ import annotations

import re
from pathlib import Path

from infra2_sdk.scaffold import main


def _get_sop_text() -> str:
    sop_path = (
        Path(__file__).resolve().parent.parent.parent
        / "docs"
        / "onboarding"
        / "07.new-service-sop.md"
    )
    assert sop_path.is_file(), f"SOP file missing: {sop_path}"
    return sop_path.read_text(encoding="utf-8")


def test_onboarding_sop_does_not_mention_deleted_generic_template() -> None:
    """The generic scaffold template was removed in SDK v3.3.0.

    The SOP must not advertise `generic` as a valid template option.
    """
    text = _get_sop_text()
    assert "generic" not in text, (
        "07.new-service-sop.md still references deleted 'generic' template"
    )


def test_onboarding_sop_scaffold_command_flags_are_valid() -> None:
    """Extract scaffold command flags from Step 1 and verify they parse cleanly."""
    text = _get_sop_text()

    # Extract the scaffold command block
    match = re.search(
        r"```bash\s*\n\s*uv run python -m infra2_sdk\.scaffold\s+(.*?)\n\s*```",
        text,
        re.DOTALL,
    )
    assert match is not None, (
        "Could not locate scaffold command block in 07.new-service-sop.md"
    )

    raw_command = match.group(1)
    # Tokenize arguments, replacing line-continuation backslashes
    tokens = [t.strip() for t in raw_command.replace("\\\n", " ").split() if t.strip()]

    # Replace placeholders with valid dummy values for argument parsing
    cleaned_tokens: list[str] = []
    for token in tokens:
        if token.startswith("<") and token.endswith(">"):
            cleaned_tokens.append("test-service")
        else:
            cleaned_tokens.append(token)

    # Dry-run parsing through main -- if invalid arguments exist, SystemExit(2) is raised
    # We pass --help or parse argv directly. Since main calls scaffold_service if parsing succeeds,
    # let's test with --help to verify flag validity, or inspect ArgumentParser directly.
    from unittest.mock import patch

    with patch("infra2_sdk.scaffold.scaffold_service") as mock_scaffold:
        mock_scaffold.return_value = []
        ret = main(cleaned_tokens)
        assert ret == 0, f"scaffold CLI returned non-zero code {ret}"
        mock_scaffold.assert_called_once()
