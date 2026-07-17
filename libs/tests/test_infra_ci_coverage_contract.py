"""Coverage visibility contracts for infra CI."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_infra_ci_publishes_coverage_context_without_blocking_delivery() -> None:
    """Infra-012.13: infra CI publishes coverage before enforcing thresholds."""
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pytest-cov" in pyproject
    assert 'source = ["libs", "tools"]' in pyproject
    assert "uv run python -m pytest libs/tests -q -n auto" in workflow
    assert 'PYTHONSAFEPATH: "1"' in workflow
    assert "--cov=libs" in workflow
    assert "--cov=tools" in workflow
    assert "--cov-report=xml:coverage/infra2-coverage.xml" in workflow
    assert "--cov-fail-under=0" in workflow
    assert "Upload infra coverage context" in workflow
    assert "infra2-coverage-context" in workflow
    assert "if-no-files-found: error" in workflow
    assert "Upload infra coverage to Coveralls" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "coverallsapp/github-action@v2" in workflow
    assert "format: cobertura" in workflow
    assert "file: coverage/infra2-coverage.xml" in workflow
    assert "coveralls.io/repos/github/wangzitian0/infra2/badge.svg?branch=main" in readme
