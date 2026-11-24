"""The IaC Runner image rebuilds when any input that its build reads changes.

Release v3.1.130 changed uv.lock (an infra2-sdk major bump). The bootstrap step ran, but the skip rule
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
            "uv.lock": "lock-a\n",
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


def _trigger_pattern() -> str:
    """The `case` pattern of reconcile-iac-inputs.yml that makes the bootstrap step run."""
    case = re.search(
        r"^\s*(bootstrap/06\.iac_runner/\*\|[^)]*)\)\s*$",
        WORKFLOW.read_text(encoding="utf-8"),
        re.M,
    )
    assert case, "the bootstrap trigger case is missing from reconcile-iac-inputs.yml"
    return case.group(1)


def _tracked_files(repo: Path, sha: str) -> list[str]:
    return _git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()


def _triggered(pattern: str, files: list[str]) -> set[str]:
    """The files that the workflow's own bash `case` sends to the bootstrap step."""
    script = f'while IFS= read -r f; do case "$f" in {pattern}) echo "$f";; esac; done'
    out = subprocess.run(
        ["bash", "-c", script],
        input="\n".join(files) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    return set(out.splitlines())


def _covered(function_text: str, repo: Path, sha: str, files: list[str]) -> set[str]:
    """The files that the fingerprint reads: a listed path, or a file below a listed directory."""
    script = function_text + f'\nrunner_inputs_fingerprint "{repo}" "{sha}"\n'
    listing = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, timeout=30
    ).stdout
    entries = [line.split("\t", 1)[1] for line in listing.splitlines()]
    return {f for f in files if any(f == e or f.startswith(e + "/") for e in entries)}


def runner_input_mismatches(pattern: str, function_text: str, repo: Path, sha: str):
    """Files that start the bootstrap step but not the fingerprint, or the reverse."""
    files = _tracked_files(repo, sha)
    triggered = _triggered(pattern, files)
    covered = _covered(function_text, repo, sha, files)
    assert triggered and covered, "the trigger or the fingerprint matches no file"
    return {
        "triggers_but_unread": sorted(triggered - covered),
        "read_but_no_trigger": sorted(covered - triggered),
    }


# The skip rule of v3.1.130: it read bootstrap/06.iac_runner only.
V3_1_130_FINGERPRINT = 'runner_inputs_fingerprint() {\n  git -C "$1" ls-tree "$2" -- bootstrap/06.iac_runner\n}\n'


def test_the_fingerprint_reads_exactly_the_files_that_trigger_the_bootstrap_step() -> (
    None
):
    """Real tree, no frozen list: every tracked file of HEAD that the workflow `case` sends to
    the bootstrap step is read by the fingerprint, and the reverse (#1863)."""
    sha = _git(ROOT, "rev-parse", "HEAD")
    result = runner_input_mismatches(_trigger_pattern(), _function_text(), ROOT, sha)
    assert result == {"triggers_but_unread": [], "read_but_no_trigger": []}


def test_the_check_reports_the_v3_1_130_skip_rule() -> None:
    sha = _git(ROOT, "rev-parse", "HEAD")
    result = runner_input_mismatches(
        _trigger_pattern(), V3_1_130_FINGERPRINT, ROOT, sha
    )
    assert "uv.lock" in result["triggers_but_unread"]
    assert "scripts/deploy_iac_runner_bootstrap.sh" in result["triggers_but_unread"]


def test_the_check_reports_a_trigger_that_the_fingerprint_does_not_read() -> None:
    sha = _git(ROOT, "rev-parse", "HEAD")
    widened = _trigger_pattern() + "|pyproject.toml"
    result = runner_input_mismatches(widened, _function_text(), ROOT, sha)
    assert result["triggers_but_unread"] == ["pyproject.toml"]
