"""The IaC Runner image rebuilds when any input that its build reads changes.

v3.1.130 changed uv.lock (infra2-sdk 4.0.0). The bootstrap step ran, but the skip rule
compared only bootstrap/06.iac_runner, so the runner kept an image with an old SDK.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy_iac_runner_bootstrap.sh"
WORKFLOW = ROOT / ".github/workflows/reconcile-iac-inputs.yml"


def _function_text() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"# BEGIN runner_inputs_fingerprint\n(.*?)# END runner_inputs_fingerprint",
        text,
        re.S,
    )
    assert match, "runner_inputs_fingerprint is missing from the bootstrap script"
    return match.group(1)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "x",
    )
    return _git(repo, "rev-parse", "HEAD")


def _fingerprint(repo: Path, sha: str) -> str:
    script = _function_text() + f'\nrunner_inputs_fingerprint "{repo}" "{sha}"\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, timeout=30
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    base = _commit(
        tmp_path,
        {
            "bootstrap/06.iac_runner/Dockerfile": "FROM python\n",
            "scripts/deploy_iac_runner_bootstrap.sh": "true\n",
            "uv.lock": 'version = "3.23.0"\n',
            "tools/sdk_requirement.py": "print(1)\n",
            "tools/other.py": "print(2)\n",
            "README.md": "a\n",
        },
    )
    return tmp_path, base


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "tools/sdk_requirement.py",
        "bootstrap/06.iac_runner/Dockerfile",
        "scripts/deploy_iac_runner_bootstrap.sh",
    ],
)
def test_a_change_to_a_build_input_changes_the_fingerprint(repo, path) -> None:
    root, base = repo
    changed = _commit(root, {path: "changed\n"})
    before, after = _fingerprint(root, base), _fingerprint(root, changed)
    assert before.strip(), "the fingerprint is empty; the skip rule would never match"
    assert before != after, path


@pytest.mark.parametrize("path", ["README.md", "tools/other.py"])
def test_a_change_outside_the_build_inputs_keeps_the_fingerprint(repo, path) -> None:
    root, base = repo
    changed = _commit(root, {path: "changed\n"})
    assert _fingerprint(root, base) == _fingerprint(root, changed), path


def test_an_unknown_commit_gives_an_empty_fingerprint_so_the_image_rebuilds(
    repo,
) -> None:
    root, _base = repo
    assert _fingerprint(root, "0" * 40) == ""
    text = SCRIPT.read_text(encoding="utf-8")
    assert (
        '[ -n "$current_runner_inputs" ] && [ "$current_runner_inputs" = "$target_runner_inputs" ]'
        in text
    )


def test_the_fingerprint_reads_the_paths_that_trigger_the_bootstrap_step() -> None:
    """One list of runner inputs: the workflow trigger and the skip rule must agree."""
    case = re.search(
        r"^\s*(bootstrap/06\.iac_runner/\*\|[^)]*)\)\s*$",
        WORKFLOW.read_text(encoding="utf-8"),
        re.M,
    )
    assert case, "the bootstrap trigger case is missing from reconcile-iac-inputs.yml"
    trigger = set(case.group(1).split("|"))
    assert trigger == {
        "bootstrap/06.iac_runner/*",
        "scripts/deploy_iac_runner_bootstrap.sh",
        "uv.lock",
        "tools/sdk_requirement*.py",
    }
    body = _function_text()
    for path in (
        "bootstrap/06.iac_runner",
        "scripts/deploy_iac_runner_bootstrap.sh",
        "uv.lock",
    ):
        assert path in body, path
    assert "tools/sdk_requirement[^/]*" in body
