"""uv.lock is the only infra2-sdk pin (#1115).

infra2-sdk re-uploaded v3.0.0 under the same URL on 2026-10-06 (#1113,
infra2-sdk#70). uv.lock caught it in CI. The runner, the alerting and todo images and
the deploy workflows installed `infra2-sdk @ <url>` with no hash and would have taken
the new bytes. Several files also spelled the URL out, and a test kept each copy
equal to pyproject.toml.

Every install outside uv now reads tools/sdk_requirement.py, which turns the
locked URL and sha256 into a requirement that pip verifies. These tests hold
that: the lock is read correctly, a bad lock fails closed, each image and each
workflow step feeds the script's output to pip whole, and no other file names the
wheel.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

from tools.sdk_requirement import sdk_requirement

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "uv.lock"
SCRIPT = ROOT / "tools/sdk_requirement.py"
WORKFLOWS = ROOT / ".github/workflows"
#: The release wheel URL or file name. The pattern's own text does not match it.
WHEEL = re.compile(r"infra2[-_]sdk/releases/download/v\d|infra2_sdk-\d+\.\d+\.\d+-py3")
#: Image builds that install infra2-sdk through the script.
IMAGES = (
    "bootstrap/06.iac_runner/Dockerfile",
    "platform/12.alerting/Dockerfile",
    "platform/30.todo/Dockerfile",
)
#: The fewest script call sites each workflow keeps. A scan that finds fewer reads
#: nothing, and the whole-requirement test would pass on an empty set.
MIN_WORKFLOW_SITES = {
    "deploy.yml": 1,
    "deploy-report-main.yml": 1,
    "infra-ci.yml": 3,
    "ops-checks.yml": 1,
    "preview-teardown.yml": 1,
    "reconcile-iac-inputs.yml": 1,
}
#: `<var>="$(python tools/sdk_requirement.py)"` with nothing after the closing quote.
#: A pipe or a `sed` there would cut the hash off.
_ASSIGNMENT = re.compile(
    r'^\s*(\w+)="\$\(python3? tools/sdk_requirement\.py\)"\s*$', re.M
)


def _locked() -> dict:
    (package,) = [
        package
        for package in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]
        if package["name"] == "infra2-sdk"
    ]
    return package


def _tracked_files() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [rel for rel in listing.split("\0") if rel and (ROOT / rel).is_file()]


def _duplicate_sdk_wheel(text: str) -> str:
    """The lock with the infra2-sdk wheel entry listed twice."""
    head, marker, tail = text.partition('name = "infra2-sdk"\nversion')
    assert marker, "the lock has no infra2-sdk package block"
    start = tail.index("wheels = [\n") + len("wheels = [\n")
    end = tail.index("\n", start) + 1
    return head + marker + tail[:end] + tail[start:end] + tail[end:]


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _workflow_sites() -> list[tuple[str, str]]:
    """(workflow file, run text) for each step that calls tools/sdk_requirement.py."""
    sites = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in (workflow.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                if "tools/sdk_requirement.py" in run:
                    # Join shell line continuations so one command is one line.
                    sites.append((path.name, run.replace("\\\n", " ")))
    return sites


def test_the_requirement_is_the_locked_wheel_and_hash() -> None:
    package = _locked()
    digest = package["wheels"][0]["hash"].removeprefix("sha256:")
    assert sdk_requirement(LOCK) == (
        f"infra2-sdk @ {package['source']['url']}#sha256={digest}"
    )


def test_the_installed_sdk_is_the_locked_release() -> None:
    assert version("infra2-sdk") == _locked()["version"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda text: text.replace('hash = "sha256:', 'hash = "md5:'), "no sha256"),
        (
            lambda text: text.replace('name = "infra2-sdk"', 'name = "other-sdk"'),
            "expected one",
        ),
        (
            lambda text: text.replace(
                'source = { url = "https://github.com/', 'source = { url = "https://x/'
            ),
            "one direct wheel",
        ),
        (_duplicate_sdk_wheel, "one direct wheel"),
    ],
    ids=["unhashed", "absent", "url-mismatch", "two-wheels"],
)
def test_a_lock_without_one_hashed_wheel_is_refused(tmp_path, mutate, message) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(mutate(LOCK.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        sdk_requirement(lock)


def test_the_cli_prints_nothing_and_fails_on_a_bad_lock(tmp_path) -> None:
    """Images and workflows run `sdk="$(...)" && pip install "$sdk"`: a failure must
    be a non-zero exit, never an empty requirement that pip skips."""
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(lock)], capture_output=True, text=True
    )
    assert (result.returncode, result.stdout) == (1, "")


@pytest.mark.parametrize("rel", IMAGES)
def test_each_image_installs_the_locked_requirement_whole(rel: str) -> None:
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "\nCOPY uv.lock tools/sdk_requirement.py /tmp/sdk/\n" in text
    flat = text.replace("\\\n", " ")  # a Dockerfile command may continue after `\`
    assert re.search(
        r'^RUN sdk="\$\(python /tmp/sdk/sdk_requirement\.py /tmp/sdk/uv\.lock\)"'
        r' +&& pip install [^\n]*"\$sdk"\s*$',
        flat,
        re.M,
    ), f"{rel} does not pass sdk_requirement.py's output to pip whole"


def test_every_image_that_installs_the_sdk_is_checked() -> None:
    """A Dockerfile that names the SDK in code, not in a comment, belongs in IMAGES."""
    installers = {
        rel
        for rel in _tracked_files()
        if Path(rel).name.startswith("Dockerfile")
        and any(
            "sdk" in line
            for line in _code_lines((ROOT / rel).read_text(encoding="utf-8"))
        )
    }
    assert installers == set(IMAGES)


def test_every_workflow_install_reads_the_locked_requirement_whole() -> None:
    sites = _workflow_sites()
    assert sites, "no workflow step calls tools/sdk_requirement.py"
    for name, run in sites:
        assigned = _ASSIGNMENT.findall(run)
        assert len(assigned) == 1, f"{name}: one plain assignment expected: {run!r}"
        var = assigned[0]
        users = [
            line
            for line in run.splitlines()
            if f"${var}" in line or f"${{{var}" in line
        ]
        assert any("pip install" in line for line in users), (
            f"{name}: no pip install uses ${var}"
        )
        for line in users:
            assert f'"${var}"' in line and f"${{{var}" not in line, (
                f"{name}: ${var} must reach pip whole and quoted: {line!r}"
            )


def test_the_expected_workflows_install_the_sdk_through_the_script() -> None:
    found = Counter(name for name, _ in _workflow_sites())
    for name, minimum in MIN_WORKFLOW_SITES.items():
        assert found[name] >= minimum, (
            f"{name}: {found[name]} step(s) call tools/sdk_requirement.py, "
            f"expected at least {minimum}"
        )


def test_only_pyproject_and_the_lock_name_the_wheel() -> None:
    offenders = []
    for rel in _tracked_files():
        if rel in ("pyproject.toml", "uv.lock") or rel.startswith("docs/project/"):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if WHEEL.search(text):
            offenders.append(rel)
    assert offenders == [], "read the pin through tools/sdk_requirement.py"
