"""`tools/app_image_imports.py` runs the entrypoint imports in the pinned App image (#1823).

The v3.1.150 escape: `dagster-entrypoint.sh` imported `truealpha_runtime.boot`, and the
image of the pinned release had no such module. The tests below stand in for the image with
the interpreter of the test run: it has the SDK that uv.lock pins and no `truealpha_runtime`,
as the image of v0.2.139 has. The job `app-image-imports` runs the same code in the real image.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from common.deploy import app_image_imports as MODULE

ROOT = Path(__file__).resolve().parents[2]

# The entrypoint line of v3.1.150 (commit 67a98a69^), the escape of #1915.
V3_1_150_ENTRYPOINT = (
    "#!/bin/sh\nset -eu\n"
    'python -c "from truealpha_runtime.boot import assert_environment; '
    "assert_environment('apps/data-engine/required-env.generated.json', "
    'require_injected=True)"\n'
    'exec "$@"\n'
)


def _local_interpreter(command, **kwargs):
    """Run the `-c` code of an image command with the interpreter of this test run."""
    code = command[command.index("-c") + 1]
    return subprocess.run([sys.executable, "-c", code], **kwargs)


def _target():
    return MODULE.ENTRYPOINT_IMAGES[0]


def test_the_v3_1_150_entrypoint_fails_the_import_check() -> None:
    problems = MODULE.check(
        _target(),
        "v0.2.139",
        V3_1_150_ENTRYPOINT,
        "docker",
        None,
        run=_local_interpreter,
    )

    assert len(problems) == 1
    assert "truealpha_runtime.boot" in problems[0]
    assert "ModuleNotFoundError" in problems[0]


def test_the_entrypoint_in_the_tree_passes_the_import_check() -> None:
    script = (ROOT / _target().script).read_text(encoding="utf-8")

    problems = MODULE.check(
        _target(), "v0.2.139", script, "docker", None, run=_local_interpreter
    )

    assert problems == []


def test_only_import_statements_run_in_the_image() -> None:
    statements = MODULE.import_statements(V3_1_150_ENTRYPOINT)

    assert statements == ["from truealpha_runtime.boot import assert_environment"]


def test_a_script_without_a_python_import_line_is_a_problem_not_a_pass() -> None:
    problems = MODULE.check(
        _target(),
        "v0.2.139",
        '#!/bin/sh\nexec "$@"\n',
        "docker",
        None,
        run=_local_interpreter,
    )

    assert len(problems) == 1
    assert "would be empty" in problems[0]


def test_the_image_command_overrides_the_entrypoint_with_python() -> None:
    command = MODULE.image_command(
        "docker", "ghcr.io/o/i:v1.2.3", "import os", "linux/amd64"
    )

    assert command == [
        "docker", "run", "--rm", "--platform", "linux/amd64",
        "--entrypoint", "python", "ghcr.io/o/i:v1.2.3", "-c", "import os",
    ]  # fmt: skip


def test_every_listed_script_exists_and_its_repository_pins_a_release_tag() -> None:
    repos = yaml.safe_load((ROOT / "harness/repos.yaml").read_text(encoding="utf-8"))
    tags = {r["id"]: r.get("release_tag") for r in repos["repositories"]}
    assert MODULE.ENTRYPOINT_IMAGES
    for target in MODULE.ENTRYPOINT_IMAGES:
        assert (ROOT / target.script).is_file(), target.script
        assert tags[target.repo_id], target.repo_id
        assert MODULE.release_tag(target.repo_id) == tags[target.repo_id]
        assert MODULE.import_statements((ROOT / target.script).read_text()), (
            target.script
        )


def test_every_entrypoint_that_runs_python_in_an_app_image_is_listed() -> None:
    """A new `python -c` entrypoint of an App stack must join ENTRYPOINT_IMAGES."""
    listed = {t.script for t in MODULE.ENTRYPOINT_IMAGES}
    found = {
        path.relative_to(ROOT).as_posix()
        for app in ("truealpha", "finance_report")
        for path in (ROOT / app).rglob("*entrypoint*.sh")
        if MODULE.import_statements(path.read_text(encoding="utf-8"))
    }
    assert found == listed
