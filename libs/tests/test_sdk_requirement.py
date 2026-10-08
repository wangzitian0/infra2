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
import shlex
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


def _with_two_wheels(text: str) -> str:
    """Repeat the infra2-sdk wheel entry: pip must not get a choice of two files."""
    block = text[text.index('name = "infra2-sdk"') :]
    entry = next(
        line
        for line in block.splitlines(keepends=True)
        if line.lstrip().startswith("{ url =") and "infra2_sdk" in line
    )
    return text.replace(entry, entry * 2, 1)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda text: text.replace('hash = "sha256:', 'hash = "md5:'), "no sha256"),
        (_with_two_wheels, "one direct wheel"),
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
    ids=["unhashed", "two-wheels", "absent", "url-mismatch"],
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


_IMAGE_ASSIGN = 'RUN sdk="$(python /tmp/sdk/sdk_requirement.py /tmp/sdk/uv.lock)"'


def _run_instruction(text: str, first_line: str) -> str:
    """The Dockerfile instruction that starts with `first_line`, on one line."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(first_line))
    end = start
    while lines[end].endswith("\\"):
        end += 1
    return " ".join(" ".join(lines[start : end + 1]).replace("\\", " ").split())


@pytest.mark.parametrize("rel", sorted(IMAGES))
def test_each_image_installs_the_locked_requirement(rel: str) -> None:
    text = (ROOT / rel).read_text(encoding="utf-8")
    # One reader: the image copies the script and the lock, and holds no reader of its own.
    assert "COPY uv.lock tools/sdk_requirement.py /tmp/sdk/\n" in text
    assert "tomllib" not in text, f"{rel} holds its own copy of the lock reader"
    run = _run_instruction(text, _IMAGE_ASSIGN)
    assert run.startswith(f"{_IMAGE_ASSIGN} && pip install "), run
    # Everything after `pip install` is pip arguments: no second command, no comment,
    # and the requirement is passed whole, once, and quoted. `${sdk%%#*}` would cut off
    # the hash, and a trailing `# "$sdk"` or `&& echo "$sdk"` would not install it.
    args = run.split(" && pip install ", 1)[1]
    assert not set(args) & set(";|&#`"), (
        f"{rel}: pip line holds a shell operator: {args}"
    )
    assert args.count("$") == 1, f"{rel}: pip line expands more than $sdk: {args}"
    # `shlex` drops the quotes, so test them in the text. An unquoted `$sdk` splits at
    # the blanks of `infra2-sdk @ <url>`: pip gets three arguments, the first without a hash.
    assert args.count('"$sdk"') == 1, f"{rel}: $sdk is not quoted: {args}"
    assert shlex.split(args).count("$sdk") == 1, f"{rel}: $sdk is not one pip argument"
    # Run the script as the image does (script, then lock path) and compare with the
    # lock read independently, so the command line is tested and not only the function.
    package = _locked()
    digest = package["wheels"][0]["hash"].removeprefix("sha256:")
    res = subprocess.run(
        [sys.executable, str(SCRIPT), str(LOCK)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == (
        f"infra2-sdk @ {package['source']['url']}#sha256={digest}"
    )


def test_every_image_that_installs_the_sdk_is_checked() -> None:
    installers = {
        rel
        for rel in _tracked_files()
        if Path(rel).name.startswith("Dockerfile")
        and "infra2-sdk" in (ROOT / rel).read_text(encoding="utf-8")
    }
    assert installers == set(IMAGES)


def test_the_script_needs_only_the_standard_library() -> None:
    """The image builds run it before pip installs anything, on a bare interpreter."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), str(LOCK)],
        capture_output=True,
        text=True,
    )
    assert (result.returncode, result.stderr) == (0, "")


#: A word that reads a file's contents. On a line with `uv.lock` it makes a second reader.
_READS_A_FILE = re.compile(
    r"\b(?:grep|sed|awk|jq|yq|cut|head|tail|tr|cat|tomllib|tomli|read_text)\b|python3? -c|open\("
)
#: Docs, tests and comments only describe the lock. The script is its one reader.
_MAY_NAME_THE_LOCK = ("uv.lock", "tools/sdk_requirement.py")


def test_only_the_script_reads_the_lock() -> None:
    offenders: list[str] = []
    lines_naming_the_lock = 0
    for rel in _tracked_files():
        if rel in _MAY_NAME_THE_LOCK or rel.startswith(("libs/tests/", "docs/")):
            continue
        if rel.endswith(".md"):
            continue
        try:
            lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            if "uv.lock" not in line or line.lstrip().startswith("#"):
                continue
            lines_naming_the_lock += 1
            if _READS_A_FILE.search(line):
                offenders.append(f"{rel}:{number}: {line.strip()[:100]}")
    # The scan must see the COPY lines and path filters that name the lock.
    assert lines_naming_the_lock >= 8, lines_naming_the_lock
    assert offenders == [], (
        f"read uv.lock through tools/sdk_requirement.py: {offenders}"
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
    assert offenders == [], (
        f"Found hardcoded wheel in {offenders}; read through uv.lock"
    )


GITHUB = ROOT / ".github"
#: The call, whole and quoted. Text after the script path inside the `$( )`, such as
#: `| sed 's/#.*//'`, would cut the hash off the requirement.
_CALL_SOURCE = r'"\$\(python3? tools/sdk_requirement\.py\)"'
_CALL = re.compile(_CALL_SOURCE)
#: `<var>="$(python tools/sdk_requirement.py)"`, alone on its line or chained with
#: `&&`, `;` or `||`.
_ASSIGNED = re.compile(r"(?<![\w$.-])(\w+)=" + _CALL_SOURCE)
_PIPE = re.compile(r"(?<!\|)\|(?!\|)")
#: `pip install`, `pip3 install`, `python -m pip -q install`, `uv pip install`.
_INSTALL = re.compile(r"\bpip3?\s+(?:-{1,2}[\w-]+(?:=\S+)?\s+)*install\b")
#: An argument that names this repository: `.`, `./`, `..`, `.[dev]`, `./[dev]`, the
#: workspace variable.
_PROJECT = re.compile(
    r"\.{1,2}/?(?:\[[^\]]*\])?|\$\{?GITHUB_WORKSPACE\}?/?|\$\{\{\s*github\.workspace\s*\}\}/?"
)
#: Words that stop the hashed install from reaching the interpreter that runs the job.
_INEFFECTIVE = (
    "--dry-run",
    "--target",
    "--prefix",
    "--root",
    "uninstall",
    "venv",
    "virtualenv",
    "set +e",
)
_SHELL_FLOW = re.compile(r"^\s*(?:if|elif|case|for|while)\b")
#: Every way to turn hash checking off, in text, env blocks included.
_SKIP_VERIFY = re.compile(r"NO_VERIFY_HASHES|--no-verify-hashes", re.I)
#: The fewest steps that call the script, per file. A scan that finds fewer reads
#: nothing, and the shape tests would pass on an empty set.
MIN_STEPS_CALLING_THE_SCRIPT = {
    "workflows/app-deploy-request.yml": 2,
    "workflows/deploy-report-main.yml": 1,
    "workflows/deploy.yml": 2,
    "workflows/infra-ci.yml": 3,
    "workflows/ops-checks.yml": 1,
    "workflows/preview-teardown.yml": 1,
    "workflows/reconcile-iac-inputs.yml": 1,
}


def _steps() -> list[tuple[str, str]]:
    """(file, shell code) of every `run` step under .github.

    That is workflows, templates and composite actions. Continuation lines are joined,
    runs of blanks collapse, and comment lines are dropped.
    """
    found = []
    for path in sorted(GITHUB.rglob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        groups = [
            job.get("steps")
            for job in (document.get("jobs") or {}).values()
            if isinstance(job, dict)
        ]
        if isinstance(document.get("runs"), dict):
            groups.append(document["runs"].get("steps"))
        for steps in groups:
            for step in steps or []:
                run = step.get("run") if isinstance(step, dict) else None
                if not run:
                    continue
                lines = run.replace("\\\n", " ").splitlines()
                code = "\n".join(
                    re.sub(r"[ \t]+", " ", line)
                    for line in lines
                    if not line.lstrip().startswith("#")
                )
                found.append((path.relative_to(GITHUB).as_posix(), code))
    return found


def _carries(line: str, variables) -> bool:
    return bool(_CALL.search(line)) or any(f'"${var}"' in line for var in variables)


def test_every_call_of_the_script_is_whole_and_reaches_pip() -> None:
    """pip checks the `#sha256=` fragment of the requirement it is given. It is the
    only protection of the wheel at install time, so a step may not cut it off."""
    sites = [
        (name, code) for name, code in _steps() if "tools/sdk_requirement.py" in code
    ]
    assert sites, "no step calls tools/sdk_requirement.py"
    for name, code in sites:
        lines = code.splitlines()
        assert len(_CALL.findall(code)) == code.count("tools/sdk_requirement.py"), (
            f"{name}: every call must be the whole quoted "
            f'"$(python tools/sdk_requirement.py)": {code!r}'
        )
        variables = sorted(set(_ASSIGNED.findall(code)))
        for var in variables:
            assigned = re.findall(rf"(?<![\w$.-]){var}=", code)
            assert len(assigned) == 1, (
                f"{name}: ${var} is assigned {len(assigned)} times; a second "
                f"assignment can cut the hash off: {code!r}"
            )
            uses = [line for line in lines if f"${var}" in line or f"${{{var}" in line]
            assert uses and all(
                f'"${var}"' in line and f"${{{var}" not in line for line in uses
            ), f"{name}: ${var} must reach pip whole and quoted: {uses!r}"
        carriers = [line for line in lines if _carries(line, variables)]
        assert not [line for line in carriers if _PIPE.search(line)], (
            f"{name}: a pipe on a line that carries the requirement can cut the hash "
            f"off: {carriers!r}"
        )
        installs = [line for line in carriers if _INSTALL.search(line)]
        assert installs, f"{name}: no `pip install` line receives the requirement"
        for line in installs:
            assert "||" not in line and not any(w in line for w in _INEFFECTIVE), (
                f"{name}: the hashed install may not fail silently or install "
                f"elsewhere: {line.strip()!r}"
            )
        for line in lines:
            assert not _SHELL_FLOW.match(line) and not any(
                w in line for w in ("uninstall", "venv", "virtualenv", "set +e")
            ), (
                f"{name}: a step that installs the SDK may not branch or undo it: {line!r}"
            )


def test_no_step_skips_the_hash_or_names_the_sdk_on_a_pip_line() -> None:
    steps = _steps()
    assert len(steps) > 50, f"the scan read only {len(steps)} run steps"
    for name, code in steps:
        for line in code.splitlines():
            assert not (
                _INSTALL.search(line) and re.search(r"infra2[-_.]sdk", line, re.I)
            ), (
                f"{name}: a pip line names the SDK; install it through "
                f"tools/sdk_requirement.py: {line.strip()!r}"
            )
    for path in sorted(GITHUB.rglob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        assert not _SKIP_VERIFY.search(text), f"{path.name}: turns hash checking off"


def _installs_the_project(line: str) -> bool:
    """True for a pip install of this repository, in any spelling."""
    if not _INSTALL.search(line):
        return False
    line = re.sub(r"\[[^\]]*\]", lambda m: m.group(0).replace(" ", ""), line)
    for token in line.split():
        token = token.strip("\"'")
        if token.startswith(("--editable=", "-e=")):
            token = token.split("=", 1)[1].strip("\"'")
        if _PROJECT.fullmatch(token):
            return True
    return False


def test_a_step_that_installs_the_project_installs_the_hashed_sdk_first() -> None:
    """`pip install -e .` pulls infra2-sdk from the URL in pyproject.toml, which has no
    hash. With the hashed wheel installed first, pip keeps it while pyproject.toml and
    uv.lock name the same URL (test_pyproject_and_the_lock_name_the_same_sdk_url). A
    swapped release asset fails the first command, and `bash -e` stops the step."""
    project_installs = 0
    for name, code in _steps():
        lines = code.splitlines()
        variables = set(_ASSIGNED.findall(code))
        for index, line in enumerate(lines):
            if not _installs_the_project(line):
                continue
            project_installs += 1
            assert not re.search(
                r"--force-reinstall|--ignore-installed|\s-I\s", line
            ), f"{name}: {line.strip()!r} would replace the hashed wheel"
            assert any(
                _INSTALL.search(earlier) and _carries(earlier, variables)
                for earlier in lines[:index]
            ), (
                f"{name}: `{line.strip()}` installs the project, and with it the SDK "
                "from the unhashed URL in pyproject.toml. Install the hashed "
                'requirement first: `pip install --no-deps "$(python '
                'tools/sdk_requirement.py)"`'
            )
    assert project_installs >= 1, "the scan found no project install: it reads nothing"


def test_pyproject_and_the_lock_name_the_same_sdk_url() -> None:
    """The hashed wheel stays only if pip later reads the same URL. A pyproject.toml
    URL that differs from uv.lock would make `pip install -e .` fetch another wheel
    and replace the verified one."""
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["dependencies"]
    (entry,) = [d for d in dependencies if d.lower().startswith("infra2-sdk")]
    url = entry.split("@", 1)[1].strip().split("#", 1)[0]
    assert url == _locked()["source"]["url"]


def test_the_expected_files_call_the_script() -> None:
    found = Counter(
        name for name, code in _steps() if "tools/sdk_requirement.py" in code
    )
    for name, minimum in MIN_STEPS_CALLING_THE_SCRIPT.items():
        assert found[name] >= minimum, (
            f"{name}: {found[name]} step(s) call tools/sdk_requirement.py, "
            f"expected at least {minimum}"
        )
