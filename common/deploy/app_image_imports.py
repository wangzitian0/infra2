#!/usr/bin/env python3
"""Run the imports of each infra2-owned App entrypoint inside the pinned App image (#1823).

infra2 writes entrypoint scripts that run inside App images. v3.1.150 shipped a data-engine
entrypoint that imported `truealpha_runtime.boot`; the image of the pinned release had no such
module, so the container looped on `ModuleNotFoundError`. A unit test that replaced the SDK
module with a fake hid it. This check pulls the image at the `release_tag` of
`harness/repos.yaml` and runs the entrypoint imports with the real interpreter of the image:

    docker run --rm --entrypoint python <image> -c "<import statements>"

Only import statements run. Calls such as `assert_environment(...)` need the Vault environment
and stay out. Usage:

The command line is `python tools/app_image_imports.py [--runtime docker|podman] [--platform linux/amd64]`.
"""

from __future__ import annotations

import ast
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.deploy.yaml_text import safe_load_text

ROOT = Path(__file__).resolve().parents[2]
REPOS_YAML = ROOT / "harness/repos.yaml"


@dataclass(frozen=True)
class EntrypointImage:
    """One infra2 script that runs inside one App image tagged with the App `release_tag`."""

    repo_id: str
    image: str
    script: str


# The one list of infra2-owned entrypoints that run in an App image. A new entrypoint with a
# `python -c` import line is added here in the same change.
ENTRYPOINT_IMAGES = (
    EntrypointImage(
        repo_id="truealpha",
        image="ghcr.io/wangzitian0/truealpha-data-engine",
        script="truealpha/truealpha/20.data_engine/dagster-entrypoint.sh",
    ),
)


def release_tag(repo_id: str, repos_yaml: Path = REPOS_YAML) -> str:
    """Return the `release_tag` that `harness/repos.yaml` pins for `repo_id`."""
    data = safe_load_text(yaml, repos_yaml.read_text(encoding="utf-8"))
    for repository in data["repositories"]:
        if repository["id"] == repo_id:
            return repository["release_tag"]
    raise SystemExit(
        f"{repo_id}: no repository entry with a release_tag in {repos_yaml}"
    )


def import_statements(script_text: str) -> list[str]:
    """Return the import statements of every `python -c "..."` line of a shell script.

    A script without such a line gives an empty list. A `python -c` payload that is not valid
    Python raises SyntaxError: the script cannot run either.
    """
    statements: list[str] = []
    for line in script_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("python -c ", "python3 -c ")):
            continue
        words = shlex.split(stripped)
        payload = words[words.index("-c") + 1]
        for node in ast.parse(payload).body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                statements.append(ast.unparse(node))
    return statements


def image_command(
    runtime: str, image_ref: str, code: str, platform: str | None
) -> list[str]:
    """Return the command that runs `code` with the interpreter of `image_ref`."""
    platform_args = ["--platform", platform] if platform else []
    return [
        runtime,
        "run",
        "--rm",
        *platform_args,
        "--entrypoint",
        "python",
        image_ref,
        "-c",
        code,
    ]


def check(
    target: EntrypointImage,
    tag: str,
    script_text: str,
    runtime: str,
    platform: str | None,
    run=subprocess.run,
) -> list[str]:
    """Return one problem string per import statement that fails in the image."""
    image_ref = f"{target.image}:{tag}"
    statements = import_statements(script_text)
    if not statements:
        return [
            f"{target.script}: no `python -c` import statement; the check would be empty"
        ]
    problems = []
    for statement in statements:
        result = run(
            image_command(runtime, image_ref, statement, platform),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            last = (result.stderr or result.stdout).strip().splitlines()[-1:]
            problems.append(
                f"{target.script}: `{statement}` fails in {image_ref}: "
                f"{last[0] if last else 'exit ' + str(result.returncode)}"
            )
    return problems
