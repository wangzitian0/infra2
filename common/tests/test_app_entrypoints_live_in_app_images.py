"""An App entrypoint script lives in its App image, never in infra2 (#1823).

v3.1.150 shipped an infra2-owned data-engine entrypoint that imported a module the pinned
image lacked, and the container looped on ModuleNotFoundError. Since truealpha v0.2.144
the image carries `/app/dagster-entrypoint.sh`, and the App repository tests it. infra2
keeps no copy, so no infra2 script can drift from the image that runs it.

### Mutation evidence
- Adding `truealpha/truealpha/20.data_engine/dagster-entrypoint.sh` back fails
  `test_no_app_stack_holds_an_entrypoint_script`.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_STACK_ROOTS = ("truealpha", "finance_report")


def entrypoint_scripts(root: Path = ROOT) -> list[str]:
    """Each `*entrypoint*.sh` file under an App stack directory of infra2."""
    return sorted(
        path.relative_to(root).as_posix()
        for app in APP_STACK_ROOTS
        for path in (root / app).rglob("*entrypoint*.sh")
    )


def test_the_app_stack_roots_exist() -> None:
    """The scan must have something to scan; a renamed directory must not pass empty."""
    for app in APP_STACK_ROOTS:
        assert (ROOT / app).is_dir(), app
        assert list((ROOT / app).rglob("compose.yaml")), app


def test_no_app_stack_holds_an_entrypoint_script() -> None:
    assert entrypoint_scripts() == []


def test_the_scan_finds_a_planted_script(tmp_path) -> None:
    stack = tmp_path / "truealpha" / "x" / "20.data_engine"
    stack.mkdir(parents=True)
    (stack / "dagster-entrypoint.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "finance_report").mkdir()
    assert entrypoint_scripts(tmp_path) == [
        "truealpha/x/20.data_engine/dagster-entrypoint.sh"
    ]
