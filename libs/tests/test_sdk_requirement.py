"""uv.lock is the only infra2-sdk pin (#1115).

infra2-sdk re-uploaded v3.0.0 under the same URL on 2026-10-06 (#1113,
infra2-sdk#70). uv.lock caught it in CI. The runner, the alerting and todo
images and the deploy workflows installed `infra2-sdk @ <url>` with no hash and
would have taken the new bytes. Four files also spelled the URL out, and a test
kept each copy equal to pyproject.toml.

Every install outside uv now derives from uv.lock, which turns the
locked URL and sha256 into a requirement that pip verifies. These tests hold
that: the lock is read correctly, a bad lock fails closed, each image feeds the
locked requirement to pip, and no other file names the wheel.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from tools.sdk_requirement import sdk_requirement

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "uv.lock"
SCRIPT = ROOT / "tools/sdk_requirement.py"
#: The release wheel URL or file name. The pattern's own text does not match it.
WHEEL = re.compile(r"infra2[-_]sdk/releases/download/v\d|infra2_sdk-\d+\.\d+\.\d+-py3")
#: Image builds that install infra2-sdk.
IMAGES = {
    "bootstrap/06.iac_runner/Dockerfile",
    "platform/12.alerting/Dockerfile",
    "platform/30.todo/Dockerfile",
}


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


def test_the_requirement_is_the_locked_wheel_and_hash() -> None:
    package = _locked()
    digest = package["wheels"][0]["hash"].removeprefix("sha256:")
    assert sdk_requirement(LOCK) == (
        f"infra2-sdk @ {package['source']['url']}#sha256={digest}"
    )


def test_the_installed_sdk_is_the_locked_release() -> None:
    assert version("infra2-sdk") == _locked()["version"]


def test_extras_are_checked_against_the_locked_release() -> None:
    assert sdk_requirement(LOCK, ("s3", "postgres")).startswith(
        "infra2-sdk[postgres,s3] @ https://"
    )
    # pip only warns about an unknown extra and installs without it.
    with pytest.raises(ValueError, match="declares no extra"):
        sdk_requirement(LOCK, ("otle",))


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
    ],
    ids=["unhashed", "absent", "url-mismatch"],
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


@pytest.mark.parametrize("rel", sorted(IMAGES))
def test_each_image_installs_the_locked_requirement(rel: str) -> None:
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "COPY uv.lock /tmp/uv.lock" in text
    run = re.search(
        r'RUN sdk="\$\((python -c ".*?")\)"\s*\\\s*&&\s*pip install .*?"\$sdk"',
        text,
        re.DOTALL,
    )
    assert run, f"{rel} does not pass inline lock reader output to pip with &&"
    cmd = run.group(1).replace("/tmp/uv.lock", str(LOCK))
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
    assert res.stdout.strip() == sdk_requirement(LOCK)


def test_every_image_that_installs_the_sdk_is_checked() -> None:
    installers = {
        rel
        for rel in _tracked_files()
        if Path(rel).name.startswith("Dockerfile")
        and "infra2-sdk" in (ROOT / rel).read_text(encoding="utf-8")
    }
    assert installers == set(IMAGES)


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
    assert offenders == [], (
        f"Found hardcoded wheel in {offenders}; read through uv.lock"
    )
