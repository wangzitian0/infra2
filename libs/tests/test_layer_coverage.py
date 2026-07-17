"""Infra-013 follow-up (truealpha CI blind spot, #503): every layer registered in
`libs.service_registry._LAYERS` must be covered by infra-ci's PR/push path filters
and by its ruff lint scope. A layer that exists in the registry but is missing from
the workflow silently skips CI/lint for that layer's changes — this test fails
closed on that gap instead of relying on someone noticing by hand."""

from __future__ import annotations

from pathlib import Path

import yaml

from libs.service_registry import _LAYERS

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_CI_PATH = REPO_ROOT / ".github/workflows/infra-ci.yml"


def _load_infra_ci() -> dict:
    # PyYAML parses the `on:` key as boolean True unless we load it as plain text
    # and only inspect string content below — avoids depending on YAML 1.1 quirks.
    return yaml.safe_load(INFRA_CI_PATH.read_text(encoding="utf-8"))


def test_every_registered_layer_is_in_the_pr_and_push_path_filters() -> None:
    workflow = _load_infra_ci()
    triggers = workflow[True]  # `on:` parses as the bool key True in YAML 1.1

    pr_paths = set(triggers["pull_request"]["paths"])
    push_paths = set(triggers["push"]["paths"])

    for layer in _LAYERS:
        expected = f"{layer}/**"
        assert expected in pr_paths, (
            f"layer {layer!r} is registered in service_registry._LAYERS but "
            f"{expected!r} is missing from infra-ci.yml pull_request.paths — "
            "changes to this layer won't trigger CI"
        )
        assert expected in push_paths, (
            f"layer {layer!r} is registered in service_registry._LAYERS but "
            f"{expected!r} is missing from infra-ci.yml push.paths"
        )


def test_every_registered_layer_is_in_the_ruff_lint_scope() -> None:
    content = INFRA_CI_PATH.read_text(encoding="utf-8")
    ruff_line = next(
        line for line in content.splitlines() if line.strip().startswith("run: ruff check")
    )
    for layer in _LAYERS:
        assert f"{layer}/" in ruff_line, (
            f"layer {layer!r} is registered in service_registry._LAYERS but is "
            f"missing from the ruff check scope: {ruff_line.strip()!r}"
        )


def test_loader_does_not_reference_a_nonexistent_finance_project() -> None:
    """Regression guard: tools/loader.py once loaded a stale `finance` project
    directory that no longer exists on disk (silently no-op'd, but drifted from
    the registry). Loader project names outside the app-layer prefix map must
    resolve to real directories."""
    loader_src = (REPO_ROOT / "tools/loader.py").read_text(encoding="utf-8")
    assert '"finance"' not in loader_src, (
        "tools/loader.py references a 'finance' project directory that no "
        "longer exists — remove it from the project list"
    )
