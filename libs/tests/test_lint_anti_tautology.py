"""Unit tests for tools/lint_anti_tautology.py (#806).

Validates detection of fake assertions:
- assert True
- assert ... or True
- not in ...[:0]
- not in empty literals
"""

from __future__ import annotations

from pathlib import Path
import pytest

from tools.lint_anti_tautology import check_source, main


def test_detects_assert_true_literal() -> None:
    code = """
def test_fake():
    assert True
"""
    violations = check_source(code)
    assert len(violations) == 1
    assert "assert True" in violations[0]


def test_detects_truthy_constant_assert() -> None:
    code = """
def test_fake():
    assert 1
    assert "always_true"
"""
    violations = check_source(code)
    assert len(violations) == 2


def test_detects_assert_or_true_disjunction() -> None:
    code = """
def test_fake():
    assert func() is None or True
"""
    violations = check_source(code)
    assert len(violations) == 1
    assert "Tautological disjunction" in violations[0]


def test_detects_assert_nested_or_true() -> None:
    code = """
def test_fake():
    assert (a == 1 and (b == 2 or True))
"""
    violations = check_source(code)
    assert len(violations) == 1
    assert "Tautological disjunction" in violations[0]


def test_detects_slice_zero_not_in() -> None:
    code = """
def test_fake():
    assert len(one) == 64 and "1" not in one[:0]
"""
    violations = check_source(code)
    assert len(violations) == 1
    assert "not in ...[:0]" in violations[0]


def test_detects_slice_zero_to_zero_not_in() -> None:
    code = """
def test_fake():
    assert "x" not in text[0:0]
"""
    violations = check_source(code)
    assert len(violations) == 1
    assert "not in ...[:0]" in violations[0]


def test_detects_empty_container_literal_not_in() -> None:
    code = """
def test_fake():
    assert item not in []
    assert item not in ()
    assert item not in ""
"""
    violations = check_source(code)
    assert len(violations) == 3


def test_permits_valid_assertions() -> None:
    code = """
def test_real():
    assert len(item) == 64
    assert item != "1" * 64
    assert "secret" not in output
    assert report.ok is True
    assert a or b
    assert x in valid_list
"""
    violations = check_source(code)
    assert violations == []


def test_permits_or_true_in_non_assert_expressions() -> None:
    # Lambdas returning True or helper dict defaults must not be flagged
    code = """
callback = lambda cls, c, env=None: seen.append(env) or True
default_val = options.get("key") or True
"""
    violations = check_source(code)
    assert violations == []


def test_cli_main_with_clean_and_dirty_files(tmp_path: Path) -> None:
    clean_file = tmp_path / "test_clean.py"
    clean_file.write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")

    dirty_file = tmp_path / "test_dirty.py"
    dirty_file.write_text("def test_bad():\n    assert True\n", encoding="utf-8")

    # Clean file exits 0
    assert main([str(clean_file)]) == 0

    # Dirty file exits 1
    assert main([str(dirty_file)]) == 1

    # Directory with dirty file exits 1
    assert main([str(tmp_path)]) == 1
