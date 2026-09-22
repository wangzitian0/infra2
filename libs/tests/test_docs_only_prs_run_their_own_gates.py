"""The gates that guard the generated doc indexes must run on doc-only changes.

Both indexes are projections of documentation:

- ``docs/project/README.md`` is generated from each ``Infra-NNN`` doc's own H1
  and Status line (``tools/gen_project_index.py``);
- ``docs/ssot/README.md``'s tables are generated from ``MANIFEST.yaml``
  (``tools/gen_ssot_index.py``).

infra-ci runs both as explicit gates -- inside ``test-deployer-logic``, whose
``if:`` is ``has_non_doc == 'true'``. A pull request that only adds, renames or
re-statuses a project doc, or that hand-edits a generated block, touches
nothing but Markdown. So the gate was skipped by precisely the change it
existed to catch, and ``test_project_index_generated.py`` -- which says in its
own docstring that "editing the README index by hand ... fails here" -- was
skipped with it. #505 (Infra-004/005 drifting to the wrong status in the index,
Infra-010/011/014/016 missing outright) would have passed CI green today.

``.github/workflows/docs.yml`` triggers on ``**/*.md``, so running the two
generators there closes it. This test states that as a property rather than as
two step names: **for every shape a change set can take, at least one workflow
that fires runs each generator.** The quadrants below cover the shapes --
Markdown or not, under ``docs/`` or not -- so no change set escapes both.

``has_non_doc`` is not re-implemented here: the classifier's own shell is
lifted out of infra-ci and executed against a real throwaway repository, so a
future edit to that shell is measured rather than assumed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA_CI = ROOT / ".github" / "workflows" / "infra-ci.yml"
DOCS = ROOT / ".github" / "workflows" / "docs.yml"

GENERATORS = ("tools/gen_project_index.py", "tools/gen_ssot_index.py")

# One per quadrant of (is Markdown?, is under docs/?), plus the two generated
# files themselves -- hand-editing those is the other way to make them stale.
CHANGE_SETS = {
    "a project doc, the #505 shape": [
        "docs/project/Infra-020.truealpha_production_datahub.md"
    ],
    "the generated project index": ["docs/project/README.md"],
    "the generated SSOT index": ["docs/ssot/README.md"],
    "Markdown outside docs/": ["README.md"],
    "non-Markdown under docs/": ["docs/ssot/MANIFEST.yaml"],
    "non-Markdown outside docs/": ["libs/env.py"],
    "a mixed change": [
        "docs/project/Infra-020.truealpha_production_datahub.md",
        "libs/env.py",
    ],
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _detect_changes_script() -> str:
    """infra-ci's own doc/non-doc classifier, ready to run."""
    jobs = yaml.safe_load(INFRA_CI.read_text(encoding="utf-8"))["jobs"]
    steps = [s for s in jobs["detect-changes"]["steps"] if s.get("run")]
    assert len(steps) == 1, f"expected one run step in detect-changes, got {steps}"
    return steps[0]["run"]


def _has_non_doc(files: list[str], tmp_path: Path) -> bool:
    """Run the real classifier over a real diff containing exactly ``files``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    # Every path must already exist at base, so the diff is a modification and
    # cannot be confused with the add/delete cases git reports differently.
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
        ["bash", "-e", str(script_path)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": subprocess.os.environ["PATH"], "GITHUB_OUTPUT": str(output)},
    )
    emitted = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return emitted["has_non_doc"] == "true"


def _to_regex(pattern: str) -> str:
    """GitHub path-filter glob: ``**`` crosses ``/``, a lone ``*`` does not."""
    assert not pattern.startswith("!"), "negated filters are not modelled here"
    assert not set(pattern) & set("?[]{}"), f"unmodelled glob syntax in {pattern!r}"
    out = []
    for token in re.split(r"(\*\*/|\*\*|\*)", pattern):
        if token == "**/":
            out.append("(?:.*/)?")
        elif token == "**":
            out.append(".*")
        elif token == "*":
            out.append("[^/]*")
        elif token:
            out.append(re.escape(token))
    return "".join(out) + r"(?:/.*)?$"


def _triggers(workflow: Path) -> dict:
    """The ``on:`` block, whichever way the file happens to spell the key.

    YAML 1.1 reads a bare ``on:`` as the boolean ``True``; this repository's
    workflows use both spellings (docs.yml bare, ops-checks.yml quoted). Asking
    for the wrong one raises rather than returning an empty trigger set, which
    would make every assertion below pass for the wrong reason.
    """
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    keys = [k for k in (True, "on") if k in doc]
    assert len(keys) == 1, f"{workflow.name} has triggers under {keys}"
    return doc[keys[0]]


def _docs_workflow_fires(files: list[str]) -> bool:
    patterns = _triggers(DOCS)["pull_request"]["paths"]
    regexes = [re.compile(_to_regex(p)) for p in patterns]
    return any(r.match(f) for f in files for r in regexes)


def _run_bodies(workflow: Path, job: str) -> str:
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    return "\n".join(s.get("run") or "" for s in jobs[job]["steps"])


@pytest.mark.parametrize("shape", sorted(CHANGE_SETS))
@pytest.mark.parametrize("generator", GENERATORS)
def test_every_change_shape_still_runs_the_index_gates(
    shape: str, generator: str, tmp_path: Path
) -> None:
    files = CHANGE_SETS[shape]
    reached = []
    if _docs_workflow_fires(files):
        reached.append(_run_bodies(DOCS, "build"))
    if _has_non_doc(files, tmp_path):
        reached.append(_run_bodies(INFRA_CI, "test-deployer-logic"))

    assert reached, f"no workflow runs at all for {shape}: {files}"
    assert any(generator in body for body in reached), (
        f"a pull request changing only {files} ({shape}) runs no check that "
        f"invokes {generator}, so it can leave that index stale and stay green"
    )
