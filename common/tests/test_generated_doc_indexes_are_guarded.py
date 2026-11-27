"""A change that can make a generated index stale must not be classed doc-only.

The indexes docs/project/README.md and docs/ssot/README.md are generated from docs
(tools/gen_project_index.py, tools/gen_ssot_index.py). The check in test_docs_lint.py
proves each index is current. It runs in infra-ci job test-deployer-logic, which
detect-changes skips for a doc-only change. These tests run the real classifier
script of detect-changes and the generators on a throwaway repository.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tools import gen_project_index, gen_ssot_index

ROOT = Path(__file__).resolve().parents[2]
INFRA_CI = ROOT / ".github" / "workflows" / "infra-ci.yml"
# The classifier step's own id, so a second `run:` step in that job cannot
# be mistaken for it.
DETECT_STEP_ID = "check"

# Nothing here should take seconds. Unbounded, a blocked subprocess holds
# `test-deployer-logic` -- a required check with no `timeout-minutes:` -- until
# GitHub's 6-hour default.
SUBPROCESS_TIMEOUT_S = 120


def _sanitized_env() -> dict[str, str]:
    """Return the ambient environment without any ``GIT_*`` variable."""
    return {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }


def _git(*args: str, cwd: Path) -> str:
    """Run git in a throwaway repository with the keys that can block pinned off."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=_sanitized_env(),
    ).stdout.strip()


def _detect_changes_script() -> str:
    """Return the run body of the detect-changes step with id ``DETECT_STEP_ID``.

    Fail when a ``shell:`` override changes the ``bash -e`` default that CI uses.
    """
    doc = yaml.safe_load(INFRA_CI.read_text(encoding="utf-8"))
    keys = [k for k in (True, "on") if k in doc]
    assert len(keys) == 1, f"infra-ci.yml has triggers under {keys}"
    job = doc["jobs"]["detect-changes"]
    steps = [s for s in job["steps"] if s.get("id") == DETECT_STEP_ID]
    assert len(steps) == 1, (
        f"expected exactly one step with id {DETECT_STEP_ID!r} in detect-changes, "
        f"got {[s.get('id') for s in job['steps']]}"
    )
    step = steps[0]
    declared = [
        step.get("shell"),
        ((job.get("defaults") or {}).get("run") or {}).get("shell"),
        ((doc.get("defaults") or {}).get("run") or {}).get("shell"),
    ]
    assert not any(declared), (
        f"a `shell:` override is in play ({declared}); `bash -e` below would no "
        "longer be what CI runs"
    )
    assert step.get("run"), f"step {DETECT_STEP_ID!r} has no run body"
    return step["run"]


def _has_non_doc(files: list[str], tmp_path: Path) -> bool:
    """Run the real classifier over a real diff containing exactly ``files``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    # Every path exists at base, so the diff is a modification and cannot be
    # confused with the add/delete cases git reports differently.
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("base\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    for name in files:
        (repo / name).write_text("head\n", encoding="utf-8")
    _git("commit", "-qam", "head", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo)

    script = _detect_changes_script()
    for expression, value in {
        "${{ github.event_name }}": "pull_request",
        "${{ github.event.pull_request.base.sha }}": base,
        "${{ github.event.pull_request.head.sha }}": head,
        "${{ github.event.before }}": base,
        "${{ github.sha }}": head,
    }.items():
        script = script.replace(expression, value)
    assert "${{" not in script, (
        "detect-changes grew a workflow expression this harness does not "
        f"substitute, so it would execute something other than CI does: {script}"
    )

    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    script_path = tmp_path / "detect.sh"
    script_path.write_text(script, encoding="utf-8")
    subprocess.run(
        # GitHub's implicit default shell for `run:` on Linux.
        ["bash", "-e", str(script_path)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=_sanitized_env() | {"GITHUB_OUTPUT": str(output)},
    )
    emitted = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return emitted["has_non_doc"] == "true"


def _generator_inputs() -> list[str]:
    """Every path a generator reads or writes, asked of the generators.

    Derived rather than listed, so adding a source directory to a generator
    cannot quietly escape the classifier rule.
    """
    paths: list[Path] = [
        gen_project_index.README,
        gen_ssot_index.MANIFEST,
        gen_ssot_index.README,
    ]
    docs = gen_project_index._project_docs(gen_project_index.PROJECT_DIR)
    assert docs, f"no project doc under {gen_project_index.PROJECT_DIR}"
    paths.append(docs[0])
    # Not a generator input, but its guard (test_project_lifecycle_governance)
    # also runs only when the classifier sees a non-doc change (#1826).
    paths.append(gen_project_index.PROJECT_DIR / "archive" / "README.md")
    paths.append(ROOT / ".agents/skills/deploy-evidence/SKILL.md")
    return [str(p.relative_to(ROOT)) for p in paths]


@pytest.mark.parametrize("path", _generator_inputs())
def test_a_generator_input_is_never_classed_doc_only(path: str, tmp_path: Path) -> None:
    """Touch any generator input and the required checks have to run."""
    assert _has_non_doc([path], tmp_path) is True, (
        f"a pull request changing only {path} is classed documentation-only, so "
        "test-deployer-logic is skipped and nothing proves the index it feeds "
        "is still in sync"
    )


def test_ordinary_prose_is_still_doc_only(tmp_path: Path) -> None:
    """The control. A rule wide enough to catch everything catches nothing.

    Without this, classing every `.md` as non-doc would pass the test above and
    make documentation-only pull requests run the full suite for a typo.
    """
    assert _has_non_doc(["docs/onboarding/02.first-app.md"], tmp_path) is False


def _tree(tmp_path: Path) -> Path:
    """A miniature repository the generators can be pointed at.

    They resolve their root from their own location, so copying them next to a
    copy of the docs they read is enough.
    """
    root = (tmp_path / "tree").resolve()
    (root / "tools").mkdir(parents=True)
    for name in ("tools/gen_project_index.py", "tools/gen_ssot_index.py"):
        shutil.copy2(ROOT / name, root / name)
    shutil.copytree(ROOT / "docs/project", root / "docs/project")
    (root / "docs/ssot").mkdir(parents=True)
    for name in ("MANIFEST.yaml", "README.md"):
        shutil.copy2(ROOT / "docs/ssot" / name, root / "docs/ssot" / name)
    return root


def _run(generator: str, root: Path) -> int:
    return subprocess.run(
        ["python3", generator],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=_sanitized_env(),
    ).returncode


def _edit_in_place(generator: str, root: Path) -> None:
    """Drift that changes no line count -- the shape a cheaper check misses."""
    if generator == "tools/gen_project_index.py":
        # The generator's own input function, not a bare glob: it skips
        # *.TODOWRITE.md and *.SUMMARY.md, and drifting one of those would
        # change nothing and make this assertion fail for the wrong reason.
        doc = gen_project_index._project_docs(root / "docs" / "project")[0]
        text = doc.read_text(encoding="utf-8")
        drifted = re.sub(
            r"^(\s*>?\s*\*\*(?:Status|状态)\*\*:?\s*).+$",
            r"\1Fixture drift",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        assert drifted != text, f"no Status line to drift in {doc.name}"
        doc.write_text(drifted, encoding="utf-8")
    else:
        manifest = root / "docs/ssot/MANIFEST.yaml"
        text = manifest.read_text(encoding="utf-8")
        drifted = re.sub(
            r"^(\s+summary: ).+$", r"\1fixture drift", text, count=1, flags=re.MULTILINE
        )
        assert drifted != text, "no summary line to drift in MANIFEST.yaml"
        manifest.write_text(drifted, encoding="utf-8")


@pytest.mark.parametrize(
    "generator", ("tools/gen_project_index.py", "tools/gen_ssot_index.py")
)
def test_a_generator_detects_drift_that_changes_no_line_count(
    generator: str, tmp_path: Path
) -> None:
    """The guarding tests only read an exit code, so the comparison must bite.

    ``test_generated_index_equals_committed`` run the
    generator and assert its exit code, which makes the generator's own
    comparison the thing being trusted. Weakening it to a cheaper proxy -- line
    counts instead of content, the kind of change that reads as an optimisation
    -- still catches an added or removed entry while missing an edit in place,
    and an edit in place is the commonest drift there is: it is what
    Infra-004/005 did in #505.
    """
    root = _tree(tmp_path)
    assert _run(generator, root) == 0, f"{generator} calls a clean tree stale"
    _edit_in_place(generator, root)
    assert _run(generator, root) != 0, (
        f"{generator} reports an index as in sync after an entry was edited in "
        "place; its comparison is not looking at content"
    )
