"""Infra-009: finance_report fixed-env rate-limit capacity contract."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "finance_report/finance_report/10.app/secrets.ctmpl"


def _rate_limit_fallback(template: str, env: str) -> str:
    default_match = re.search(
        r'\$api_rate_limit_requests\s*:=\s*"(?P<value>\d+)"',
        template,
    )
    assert default_match is not None
    fallback = default_match.group("value")

    override_pattern = re.compile(
        r'if\s+eq\s+\$env\s+"(?P<env>[^"]+)".*?'
        r'\$api_rate_limit_requests\s*=\s*"(?P<value>\d+)".*?end',
        re.DOTALL,
    )
    for match in override_pattern.finditer(template):
        if match.group("env") == env:
            return match.group("value")
    return fallback


def test_finance_report_rate_limit_fallback_is_environment_specific() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    assert _rate_limit_fallback(template, "staging") == "2000"
    assert _rate_limit_fallback(template, "production") == "300"
    assert _rate_limit_fallback(template, "unexpected") == "300"


def test_vault_rate_limit_value_keeps_precedence_over_the_fallback() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    assignment = re.search(
        r"API_RATE_LIMIT_REQUESTS=\{\{\s*with\s+"
        r"\.Data\.data\.API_RATE_LIMIT_REQUESTS\s*\}\}"
        r".*?\{\{\s*else\s*\}\}"
        r'\{\{\s*\$api_rate_limit_requests\s*\|\s*printf\s+"%q"\s*\}\}'
        r"\{\{\s*end\s*\}\}",
        template,
    )

    assert assignment is not None
