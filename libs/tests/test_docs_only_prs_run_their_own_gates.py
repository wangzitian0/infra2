"""The gates that guard the generated doc indexes must run, and must still bite.

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
generators there closes it.

Two claims have to hold, and each is measured rather than read:

**Reachability** -- for every file either generator derives its answer from, a
workflow that fires on a change to that file runs a step invoking it. The
firing model covers both workflows' own ``paths:`` filters *and* infra-ci's
``has_non_doc`` gate, whose classifier shell is lifted out of the workflow and
executed against a throwaway repository rather than re-implemented.

**Bite** -- that step, run for real against a tree where the index is stale,
exits non-zero. Asserting the generator's *name* appears in the step text
proves nothing: appending ``|| true`` leaves every such assertion green while
the gate stops gating, which is the exact shape #758's sibling test was
written to refuse ("the exit code is closed where the text is open"). The step
is therefore executed, with ``uv`` shimmed to plain ``python3`` so the shell
logic around the command -- ``&&``, ``||``, ``set +e``, an early ``exit 0`` --
is what is being measured. ``continue-on-error`` is the one way to neuter a
step from outside its script; it is a YAML key with a single meaning, so it is
checked as one.

Scope note: the claim is about the files these two generators read, not about
every conceivable change set. infra-ci's ``paths:`` is a curated allowlist, so
some paths (``pyproject.toml``, ``uv.lock``) trigger neither workflow -- those
cannot make either index stale, and ``pr_merge_gate`` blocks such a pull
request anyway under "required check(s) never reported".
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
DOCS = ROOT / ".github" / "workflows" / "docs.yml"

# Which job each workflow runs the generators from, and (for infra-ci) whether
# that job is behind the doc/non-doc classifier.
# (workflow, job, the job's `if:` exactly as the workflow must spell it).
# None means the job is unconditional. Pinned rather than evaluated: running a
# GitHub expression would mean reimplementing its language here, and a wrong
# reimplementation is a worse oracle than a mismatch that demands a human
# update this line. It exists because a blind audit set `if: false` on each of
# these jobs in turn -- the whole job disabled, and for test-deployer-logic
# that is a dozen required gates, not two -- and every test here stayed green:
# the suite proved "if these steps ran they would behave", never "this job runs".
GATE_JOBS = (
    (DOCS, "build", None),
    (
        INFRA_CI,
        "test-deployer-logic",
        "needs.detect-changes.outputs.has_non_doc == 'true'",
    ),
)

# The complete set of ways one step can change a later step's environment in
# the same job: GitHub defines exactly these (the two command files, plus the
# deprecated workflow commands they replaced). Unlike shell verbs, this is a
# closed set fixed by the platform, so scanning for it is not the open-set
# trap. A step writing any of them before a gate can shadow `uv` or `python3`
# for every step after it -- which a harness that executes one step in
# isolation cannot see, and a blind audit duly walked through.
CROSS_STEP_ENV_MECHANISMS = ("GITHUB_PATH", "GITHUB_ENV", "add-path", "set-env")

# Every file a generator derives its answer from. Changing one of these is what
# makes the corresponding index stale, so each must reach a gate.
GENERATOR_INPUTS = {
    "tools/gen_project_index.py": (
        "docs/project/Infra-020.truealpha_production_datahub.md",  # a project doc
        "docs/project/README.md",  # the generated file, hand-edited
    ),
    "tools/gen_ssot_index.py": (
        "docs/ssot/MANIFEST.yaml",
        "docs/ssot/README.md",  # the generated file, hand-edited
    ),
}

# `uv run python x` -> `python3 x`. Everything else in the step's script runs
# verbatim, so the shell around the command is what is measured.
# Nothing here should take seconds, let alone minutes. Without a bound, a
# subprocess that blocks holds `test-deployer-logic` -- a required check with
# no `timeout-minutes:` -- until GitHub's 6-hour default. Inheriting the
# ambient environment (which is right, see _sanitized_env) is what makes that
# reachable: a global git config can point `commit.gpgsign` at a gpg that waits
# on a pinentry with no TTY.
SUBPROCESS_TIMEOUT_S = 120

UV_SHIM = """#!/bin/sh
[ "$1" = "run" ] && shift
[ "$1" = "python" ] && shift
exec python3 "$@"
"""


def _sanitized_env() -> dict[str, str]:
    """The ambient environment, minus the three variables that would send git
    somewhere other than the repository under test.

    Inherited rather than rebuilt: a step on a runner gets a populated
    environment, and a hand-picked PATH-only one is not a smaller version of
    that but a different one, where git and grep can behave differently (HOME,
    locale) and a false verdict here would be invisible.

    The exception is the whole ``GIT_*`` family rather than a list of names.
    Three were stripped at first -- ``GIT_DIR``, ``GIT_WORK_TREE``,
    ``GIT_INDEX_FILE`` -- and an audit pointed out the list was open:
    ``GIT_OBJECT_DIRECTORY``, ``GIT_COMMON_DIR``, ``GIT_CONFIG_GLOBAL``,
    ``GIT_CEILING_DIRECTORIES`` redirect git just as effectively, and
    ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` beat the ``-c user.*`` pins outright
    because git reads identity from the environment ahead of config. Nothing
    here needs any of them, and a prefix is closed where a list is open.
    """
    return {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }


def _git(*args: str, cwd: Path) -> str:
    """git in a throwaway repository, deaf to the ambient user's configuration.

    Identity, signing, hooks and the filesystem monitor are pinned rather than
    inherited. ``_sanitized_env`` deliberately keeps ``HOME``, so
    ``~/.gitconfig`` is in play, and each of these can block: a
    ``commit.gpgsign = true`` whose gpg waits on a pinentry with no TTY, a
    ``core.hooksPath`` holding a pre-commit hook, or a ``core.fsmonitor``
    pointing at a daemon that stopped answering -- the last one measured
    blocking ``git add -A`` with the other three pins already in place.
    Identity is pinned here *and* by dropping ``GIT_AUTHOR_*``/
    ``GIT_COMMITTER_*`` in ``_sanitized_env``, since those beat ``-c``.
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


def _jobs(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]


def _triggers(workflow: Path) -> dict:
    """The ``on:`` block, whichever way the file happens to spell the key.

    YAML 1.1 reads a bare ``on:`` as the boolean ``True``; this repository's
    workflows use both spellings (docs.yml bare, infra-ci.yml bare,
    ops-checks.yml quoted). Asking for the wrong one would return an empty
    trigger set and make every assertion below pass for the wrong reason.
    """
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    keys = [k for k in (True, "on") if k in doc]
    assert len(keys) == 1, f"{workflow.name} has triggers under {keys}"
    return doc[keys[0]]


def _detect_changes_script() -> str:
    """infra-ci's own doc/non-doc classifier, ready to run."""
    steps = [s for s in _jobs(INFRA_CI)["detect-changes"]["steps"] if s.get("run")]
    assert len(steps) == 1, f"expected one run step in detect-changes, got {steps}"
    return steps[0]["run"]


def _expand(script: str, substitutions: dict[str, str]) -> str:
    for expression, value in substitutions.items():
        script = script.replace(expression, value)
    assert "${{" not in script, (
        "the step grew a workflow expression this harness does not substitute, "
        f"so it would execute something other than CI does: {script}"
    )
    return script


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

    script = _expand(
        _detect_changes_script(),
        {
            "${{ github.event_name }}": "pull_request",
            "${{ github.event.pull_request.base.sha }}": base,
            "${{ github.event.pull_request.head.sha }}": head,
            "${{ github.event.before }}": base,
            "${{ github.sha }}": head,
        },
    )
    output = tmp_path / "gh_output"
    output.write_text("", encoding="utf-8")
    script_path = tmp_path / "detect.sh"
    script_path.write_text(script, encoding="utf-8")
    env = _sanitized_env() | {"GITHUB_OUTPUT": str(output)}
    subprocess.run(
        ["bash", "-e", str(script_path)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=env,
    )
    emitted = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return emitted["has_non_doc"] == "true"


def _to_regex(pattern: str) -> str:
    """GitHub path-filter glob: ``**`` crosses ``/``, a lone ``*`` does not.

    The whole path must match. There is no implicit "...and everything under
    it" -- which is exactly why workflows write ``docs/**`` and not ``docs``.
    Modelling a trailing ``/...`` as optional would make this matcher more
    permissive than GitHub: it would report a workflow as firing for a pattern
    GitHub would not match, and a `paths:` list narrowed by mistake would then
    still look covered here.

    ``**/`` stands for zero or more directories, so ``**/*.md`` matches a path
    with no ``/`` in it at all. That is not read off the documentation: #773
    changed only ``AGENTS.md`` -- root level, no directory part, nothing under
    ``docs/`` -- and ``Docs / build`` ran on it.
    """
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
    return "".join(out)


# What the matcher must and must not say, so the model itself is guarded
# rather than trusted. The first case is the observed one (#773).
_GLOB_CASES = (
    ("**/*.md", "AGENTS.md", True),
    ("**/*.md", "docs/ssot/README.md", True),
    ("**/*.md", "docs/ssot/MANIFEST.yaml", False),
    ("docs/**", "docs/ssot/README.md", True),
    ("docs/**", "docsite/x.md", False),
    # A bare directory name is not a prefix filter -- the reason `docs/**` is
    # spelled out. A matcher that got this wrong would call a narrowed
    # `paths:` list covered.
    ("docs", "docs/ssot/README.md", False),
    (".github/workflows/docs.yml", ".github/workflows/docs.yml", True),
    (".github/workflows/docs.yml", ".github/workflows/docs.yml.bak", False),
    ("*.md", "docs/x.md", False),
)


@pytest.mark.parametrize(("pattern", "path", "expected"), _GLOB_CASES)
def test_the_path_filter_model_matches_github(
    pattern: str, path: str, expected: bool
) -> None:
    assert bool(re.fullmatch(_to_regex(pattern), path)) is expected


def _workflow_fires(workflow: Path, files: list[str]) -> bool:
    patterns = _triggers(workflow)["pull_request"]["paths"]
    regexes = [re.compile(_to_regex(p)) for p in patterns]
    return any(r.fullmatch(f) for f in files for r in regexes)


def _gate_steps(generator: str, files: list[str], tmp_path: Path) -> list[tuple]:
    """(workflow, job name, step) for every gate a change to ``files`` reaches."""
    reached = []
    for workflow, job_name, expected_if in GATE_JOBS:
        if not _workflow_fires(workflow, files):
            continue
        if expected_if is not None:
            scratch = tmp_path / workflow.stem
            scratch.mkdir(parents=True, exist_ok=True)
            if not _has_non_doc(files, scratch):
                continue
        job = _jobs(workflow)[job_name]
        reached += [
            (workflow, job_name, index, step)
            for index, step in enumerate(job["steps"])
            if generator in (step.get("run") or "")
        ]
    return reached


@pytest.mark.parametrize(
    ("workflow", "job_name", "expected_if"),
    GATE_JOBS,
    ids=[f"{w.name}:{j}" for w, j, _ in GATE_JOBS],
)
def test_the_gate_job_still_runs_when_the_model_says_it_does(
    workflow: Path, job_name: str, expected_if: str | None
) -> None:
    """The job hosting a gate must be scheduled on the terms the model assumes.

    Everything else here executes a step's script directly, which says nothing
    about whether GitHub would ever start the job holding it. Disabling the job
    -- a mistyped condition, a renamed `needs` output, a stray `false` -- turns
    the gate off while every other assertion stays green.
    """
    job = _jobs(workflow)[job_name]
    assert job.get("if") == expected_if, (
        f"{workflow.name}:{job_name} runs under `if: {job.get('if')!r}` but "
        f"GATE_JOBS assumes {expected_if!r}. Either the job's scheduling "
        "changed and the model has to follow, or the condition was broken."
    )


def _tree(tmp_path: Path, *, stale_for: str | None, mode: str = "add") -> Path:
    """A miniature repository, optionally with ``stale_for``'s index out of date.

    The generators resolve their root from their own location
    (``Path(__file__).resolve().parents[1]``), so copying them next to a copy
    of the docs they read is enough to point them at this tree.
    """
    root = (tmp_path / (f"stale-{mode}" if stale_for else "clean")).resolve()
    if root.exists():
        return root
    (root / "tools").mkdir(parents=True)
    for name in GENERATOR_INPUTS:
        shutil.copy2(ROOT / name, root / name)
    shutil.copytree(ROOT / "docs/project", root / "docs/project")
    (root / "docs/ssot").mkdir(parents=True)
    for name in ("MANIFEST.yaml", "README.md"):
        shutil.copy2(ROOT / "docs/ssot" / name, root / "docs/ssot" / name)

    if stale_for is None:
        return root
    if stale_for == "tools/gen_project_index.py" and mode == "status":
        # The other half of #505: not a missing doc, but an existing one whose
        # Status drifted from what the index says. Infra-004/005 showed both
        # shapes, and only the first had a fixture until an audit said so.
        doc = root / "docs" / "project" / GENERATOR_INPUTS[stale_for][0].split("/")[-1]
        text = doc.read_text(encoding="utf-8")
        drifted = re.sub(
            r"^(\s*>?\s*\*\*(?:Status|状态)\*\*:?\s*).+$",
            r"\1Audit fixture drift",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        assert drifted != text, f"no Status line to drift in {doc.name}"
        doc.write_text(drifted, encoding="utf-8")
    elif stale_for == "tools/gen_project_index.py":
        # A new project doc the committed index cannot know about. Built from
        # segments on purpose: this file never exists, and spelling it as one
        # literal would make test_code_doc_path_references_resolve read it as a
        # reference to a real document and fail.
        fixture = root / "docs" / "project" / "Infra-999.stale_fixture.md"
        fixture.write_text(
            "# Infra-999: stale fixture\n\n**Status**: In Progress\n", encoding="utf-8"
        )
    else:
        # A hand edit inside the generated block -- the thing the header of
        # docs/ssot/README.md forbids.
        #
        # The marker comes from the generator, and the edit goes after the
        # WHOLE marker line. The first version of this fixture partitioned on a
        # prefix of it and so cut the marker in half; the generator then failed
        # with "markers not found" -- the deleted-marker path, byte-identical
        # to deleting the marker outright, not the hand-edit path this is
        # supposed to exercise. Same exit code, different reason, and every
        # assertion downstream passed while proving the wrong thing.
        readme = root / "docs/ssot/README.md"
        head, marker, rest = readme.read_text(encoding="utf-8").partition(
            gen_ssot_index.BEGIN
        )
        assert marker, "the generated SSOT block's BEGIN marker moved"
        readme.write_text(f"{head}{marker}\n| hand edited |{rest}", encoding="utf-8")
    return root


def _markers_intact(generator: str, root: Path) -> None:
    """A generator that cannot find its own markers fails for a reason that has
    nothing to do with staleness, so the fixture must leave them alone."""
    if generator == "tools/gen_ssot_index.py":
        text = (root / "docs/ssot/README.md").read_text(encoding="utf-8")
        required = (gen_ssot_index.BEGIN, gen_ssot_index.END)
    else:
        text = (root / "docs/project/README.md").read_text(encoding="utf-8")
        required = (
            gen_project_index.BEGIN_ACTIVE,
            gen_project_index.END_ACTIVE,
            gen_project_index.BEGIN_ARCHIVED,
            gen_project_index.END_ARCHIVED,
        )
    for marker in required:
        assert marker in text, (
            f"the fixture damaged {marker!r}, so {generator} would fail on "
            "'markers not found' instead of on a stale index"
        )


# Each way an index can go out of date, so a regression confined to one of
# them cannot hide behind a fixture that only exercises the other.
_STALE_MODES = (
    ("tools/gen_project_index.py", "add"),
    ("tools/gen_project_index.py", "status"),
    ("tools/gen_ssot_index.py", "hand-edit"),
)


@pytest.mark.parametrize(("generator", "mode"), _STALE_MODES)
def test_the_stale_fixture_is_actually_stale(
    generator: str, mode: str, tmp_path: Path
) -> None:
    """If the fixture did not make the index stale, every bite test below would
    pass without proving anything."""
    root = _tree(tmp_path, stale_for=generator, mode=mode)
    _markers_intact(generator, root)
    result = subprocess.run(
        ["python3", generator],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=_sanitized_env(),
    )
    assert result.returncode != 0, f"{generator} reported a stale tree as fine"
    other = next(g for g in GENERATOR_INPUTS if g != generator)
    paired = subprocess.run(
        ["python3", other],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=_sanitized_env(),
    )
    # Only the targeted index is stale, so a step that runs both must be
    # failing because of this one -- which is what proves the failure
    # propagates through the step rather than coming from its sibling.
    assert paired.returncode == 0, f"{other} is also stale; the fixture is not isolated"


def _run_step(step: dict, root: Path, env: dict[str, str]) -> int:
    """Exit code of the step's own script, run against ``root``."""
    script = root / "step.sh"
    script.write_text(
        _expand(step["run"], {"${{ github.workspace }}": str(root)}), encoding="utf-8"
    )
    return subprocess.run(
        ["bash", "-e", str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        env=env,
    ).returncode


@pytest.mark.parametrize("generator", sorted(GENERATOR_INPUTS))
@pytest.mark.parametrize("which_input", (0, 1))
@pytest.mark.parametrize("mixed", (False, True), ids=("docs-only", "with-code"))
def test_every_generator_input_reaches_a_gate_that_bites(
    generator: str, which_input: int, mixed: bool, tmp_path: Path
) -> None:
    """Every gate a change reaches must pass on a fresh index and fail on a stale one.

    Both halves are load-bearing, and each was added because a mutation beat
    the version without it:

    - without the fresh-index half, replacing the step body with a bare
      ``exit 1`` -- the gate removed outright, every Markdown pull request red
      regardless of the index -- satisfied the staleness assertion and all
      four cases passed. ``returncode != 0`` is satisfied by a step that has
      stopped checking anything.
    - without ``mixed``, every input for ``gen_project_index`` was pure
      Markdown, so ``has_non_doc`` was false and infra-ci's copy of the gate
      was never reached. Putting ``|| true`` on it -- the exact pattern this
      file's docstring says it refuses -- left all 15 tests green.

    And the verdict is taken over *every* gate reached, not any of them: a
    reached step that names the generator and swallows its exit code is a lie
    whether or not a sibling gate happens to cover the same change.
    """
    files = [GENERATOR_INPUTS[generator][which_input]]
    if mixed:
        # A non-Markdown file, so has_non_doc is true and infra-ci's own copy
        # of the gate is reached as well as docs.yml's.
        files.append("libs/env.py")
    reached = _gate_steps(generator, files, tmp_path)
    assert reached, (
        f"a pull request changing only {files} runs no check that invokes "
        f"{generator}, so it can leave that index stale and stay green"
    )

    clean = _tree(tmp_path, stale_for=None)
    stale = _tree(tmp_path, stale_for=generator)
    _markers_intact(generator, stale)
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "uv").write_text(UV_SHIM, encoding="utf-8")
    (shim / "uv").chmod(0o755)
    env = _sanitized_env() | {"PATH": f"{shim}:{os.environ['PATH']}"}

    for workflow, job_name, index, step in reached:
        where = f"{workflow.name}:{job_name} step {step.get('name')!r}"
        job = _jobs(workflow)[job_name]
        for earlier in job["steps"][:index]:
            body = earlier.get("run") or ""
            hijacks = [m for m in CROSS_STEP_ENV_MECHANISMS if m in body]
            assert not hijacks, (
                f"{workflow.name}:{job_name} step {earlier.get('name')!r} runs "
                f"before {step.get('name')!r} and writes {hijacks} -- it can "
                "put a shadow `uv` or `python3` ahead of the real one for every "
                "step after it, which this harness cannot see because it runs "
                "one step in isolation rather than replaying the job"
            )
        assert not step.get("continue-on-error"), (
            f"{where} is continue-on-error, so its verdict cannot fail the job"
        )
        assert not job.get("continue-on-error"), (
            f"{workflow.name}:{job_name} is continue-on-error"
        )

        on_fresh = _run_step(step, clean, env)
        assert on_fresh == 0, (
            f"{where} exits {on_fresh} against a tree whose indexes are already "
            "in sync -- it is not gating, it is just red, and an always-red "
            "step satisfies the staleness check below without checking anything"
        )
        on_stale = _run_step(step, stale, env)
        assert on_stale != 0, (
            f"{where} ran against a tree whose {generator} index is stale and "
            "still reported success -- it names the generator but no longer "
            "lets its verdict out"
        )
