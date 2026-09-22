"""A change that can make a generated index stale must not be classed doc-only.

Two indexes are projections of documentation:

- ``docs/project/README.md`` from each ``Infra-NNN`` doc's H1 and Status line
  (``tools/gen_project_index.py``)
- ``docs/ssot/README.md`` from ``MANIFEST.yaml`` (``tools/gen_ssot_index.py``)

``test_project_index_generated`` and ``test_ssot_index_generated`` already prove
each index is in sync, and they run in infra-ci's ``test-deployer-logic`` --
one of the seven checks GitHub's ruleset requires with no bypass actors. The
defect was never those tests; it was that the job hosting them is gated on
``has_non_doc``, and a pull request that adds, renames or re-statuses a project
doc, or hand-edits a generated block, is all Markdown. The change that makes an
index stale was the change that skipped the test proving it is not. #505 is the
recorded instance: Infra-004/005 showing the wrong status, Infra-010/014/016
missing outright.

So the fix is one rule in ``detect-changes``, and this file proves the two
things that rule has to get right, plus the one property the guarding tests
rest on.

**Why this shape.** The first attempt instead added the generators to
``docs.yml`` and modelled CI to prove that step would run and bite: path
filters, the job's ``if:``, ``needs:``, ``shell:``, preceding ``uses:`` steps.
Six rounds of adversarial audit defeated it nineteen distinct ways -- step and
job ``if:``, ``working-directory``, ``strategy.matrix``, ``container``,
``services``, ``environment``, ``concurrency``, ``runs-on``, ``permissions``,
``timeout-minutes``, ``env``, ``defaults``, ``with: {ref: main}`` on checkout,
``on.pull_request.types``, a mistyped step id in ``outputs:`` -- because its
input was the GitHub Actions schema, which is not a set anyone finishes
enumerating. Here the input is the classifier's own shell and the generators'
own behaviour. Whether the job runs is GitHub's problem, and GitHub is the one
component that cannot get it wrong.
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

# Nothing here should take seconds. Unbounded, a blocked subprocess holds
# `test-deployer-logic` -- a required check with no `timeout-minutes:` -- until
# GitHub's 6-hour default.
SUBPROCESS_TIMEOUT_S = 120


def _sanitized_env() -> dict[str, str]:
    """The ambient environment, minus every ``GIT_*``.

    Inherited rather than rebuilt: a step on a runner gets a populated
    environment, and a hand-picked PATH-only one is not a smaller version of
    that but a different one, where git and grep can behave differently.
    The exception is a prefix rather than a list of names, because the list is
    open -- ``GIT_DIR`` and ``GIT_CONFIG_GLOBAL`` redirect git, and
    ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` beat the ``-c`` pins below outright.
    """
    return {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }


def _git(*args: str, cwd: Path) -> str:
    """git in a throwaway repository, deaf to the ambient user's configuration.

    ``HOME`` is kept on purpose, so ``~/.gitconfig`` is in play and each of
    these can block: a ``commit.gpgsign`` whose gpg waits on a pinentry with no
    TTY, a ``core.hooksPath`` holding a pre-commit hook, a ``core.fsmonitor``
    pointing at a daemon that stopped answering -- the last measured blocking
    ``git add -A`` with the other pins already in place.
    """
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
    """infra-ci's own doc/non-doc classifier, lifted out rather than restated."""
    doc = yaml.safe_load(INFRA_CI.read_text(encoding="utf-8"))
    keys = [k for k in (True, "on") if k in doc]
    assert len(keys) == 1, f"infra-ci.yml has triggers under {keys}"
    steps = [s for s in doc["jobs"]["detect-changes"]["steps"] if s.get("run")]
    assert len(steps) == 1, f"expected one run step in detect-changes, got {steps}"
    return steps[0]["run"]


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
    for directory in (gen_project_index.PROJECT_DIR, gen_project_index.ARCHIVE_DIR):
        docs = gen_project_index._project_docs(directory)
        assert docs, f"no project doc under {directory}"
        paths.append(docs[0])
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

    ``test_project_index_generated`` and ``test_ssot_index_generated`` run the
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
