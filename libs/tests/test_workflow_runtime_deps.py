"""A job that hand-writes `pip install` must install what its entry module needs.

#783 wired the pre-deploy schema gate into `deploy_v2`, which made
`tools/preview_leak_check.py` -- importing `deploy_v2` for a single integer --
transitively import `yaml`. The nightly preview-leak job installs
`httpx python-dotenv rich`, so it died at `ModuleNotFoundError: No module named
'yaml'` every hour. Nothing noticed: `ops-checks.yml` runs that job only on a
schedule, so no pull request has ever exercised it.

The check asks the closed question -- *does the import succeed with exactly the
declared distributions* -- by importing the module for real with everything else
blocked, and it raises the same `ModuleNotFoundError` the runner would.

It is deliberately not a static model. An AST walk disagreed with the CI failure
it was meant to explain three times running (`from libs.deploy import schema_gate`
resolving to a package `__init__` rather than the submodule; `if TYPE_CHECKING:`
counted as a runtime import; imports inside functions counted too). Nor is it
"what does `sys.modules` hold after importing with everything installed": that
over-reports every optional import behind a `try/except ImportError`, which is
exactly how it wrongly accused a job that is green in production.

Scope: only jobs that run `python -m` directly. A step under `uv run` takes its
dependencies from `pyproject.toml`, which is not a hand-written list and does not
go stale the same way.
"""

from __future__ import annotations

import json
import subprocess
import sys
import re
from importlib.metadata import PackageNotFoundError, packages_distributions, requires
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
# Provided by the checkout, not by pip.
REPO_PACKAGES = ("libs", "tools", "finance_report", "truealpha")

# Run in the subprocess: block every top-level module the job does not install, then
# import. `find_spec` raising ModuleNotFoundError is what a genuinely absent module
# does, so a `try/except ImportError` fallback still takes its fallback -- which is
# the behaviour being checked, not a case to special-case.
_PROBE = """
import json, os, sys

# The jobs run `python -m tools.X` from the repo root, where the root is on
# sys.path. This probe can itself run under PYTHONSAFEPATH=1 (infra-ci sets it),
# which drops the implicit cwd entry -- so put it back explicitly. Without this
# the probe reports the repo's own packages as missing, which is a fact about
# the probe and not about the job it is meant to be judging.
sys.path.insert(0, os.getcwd())

ALLOWED = set(json.loads(sys.argv[1])) | set(sys.stdlib_module_names)


class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".")[0]
        if top in ALLOWED or top in sys.modules:
            return None
        raise ModuleNotFoundError(f"No module named {top!r}", name=top)


sys.meta_path.insert(0, Blocker())
try:
    __import__(sys.argv[2])
except ModuleNotFoundError as exc:
    print(json.dumps({"missing": exc.name}))
else:
    print(json.dumps({"missing": None}))
"""


def _install_lines(job: dict) -> list[str]:
    out = []
    for step in job.get("steps") or []:
        run = str(step.get("run") or "").replace("\\\n", " ")
        out += [line for line in run.splitlines() if "pip install" in line]
    return out


# `name @ url` inside a shell assignment, e.g.
# `sdk_requirement="$(... value.startswith("infra2-sdk @ ") ...)"`.
_PINNED_IN_SHELL = re.compile(r"([A-Za-z][A-Za-z0-9._-]*)\s+@\s")
# `$( ... )`, with one level of nested parentheses -- enough for the shapes here.
_CMD_SUBST = re.compile(r"\$\((?:[^()]|\([^()]*\))*\)")


def _declared_distributions(job: dict) -> set[str] | None:
    """Distributions a job's `pip install` names, or None when it is not a list.

    None for `pip install .` / `-e .` / `-r requirements.txt`: those take the
    dependency set from a file that moves with the code, which is the thing a
    hand-written list fails to do. Checking them would assert nothing.
    """
    lines = _install_lines(job)
    if not lines:
        return None
    run_text = "\n".join(
        str(step.get("run") or "") for step in (job.get("steps") or [])
    )
    found: set[str] = set()
    for line in lines:
        tail = line.split("pip install", 1)[1]
        # `"$(grep -oE 'infra2-sdk @ https://...' pyproject.toml)"` names a
        # distribution only a shell can resolve. Read the name out of it and take
        # the substitution off the line: splitting on whitespace first turned the
        # grep's own arguments into package names (`https://`, `pyproject.toml)`).
        for subst in _CMD_SUBST.findall(tail):
            found |= set(_PINNED_IN_SHELL.findall(subst))
            tail = tail.replace(subst, " ")
        for token in tail.split():
            token = token.strip("\"'")
            if token in ("\\", ""):
                continue
            if token in (".", "-e") or token.startswith(("-r", "--requirement")):
                return None
            if token.startswith("-"):
                continue
            if token.startswith("$"):
                # A shell variable: resolve it from its assignment in the same
                # step rather than dropping it, which used to read as "this job
                # installs nothing called that" and accuse it of the gap.
                name = token.lstrip("${").rstrip("}")
                for assignment in run_text.splitlines():
                    if assignment.strip().startswith(f"{name}="):
                        found |= set(_PINNED_IN_SHELL.findall(assignment))
                continue
            spec = token.split("@")[0].split("==")[0].split(">")[0].split("[")[0]
            if spec.strip():
                found.add(spec.strip())
    return found or None


def _entry_modules(job: dict) -> set[str]:
    """Repo modules run as `python -m`, excluding anything under `uv run`."""
    found: set[str] = set()
    for step in job.get("steps") or []:
        run = str(step.get("run") or "").replace("\\\n", " ")
        for line in run.splitlines():
            if "uv run" in line:
                continue
            tokens = line.split()
            for index, token in enumerate(tokens):
                if token == "-m" and index + 1 < len(tokens):
                    candidate = tokens[index + 1]
                    if candidate.startswith(REPO_PACKAGES):
                        found.add(candidate)
    return found


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _dependency_closure(distributions: set[str]) -> set[str]:
    """`pip install httpx` also installs idna, certifi, httpcore, anyio, sniffio.

    Read from installed metadata rather than listed here, for the same reason the
    workflows' own lists are the thing under test. Requirements carrying an
    environment marker are skipped: an `extra ==` requirement is not installed by a
    bare `pip install <dist>`, and counting it would let a job declare less than it
    needs -- the failure direction this file exists to prevent.
    """
    seen: set[str] = set()
    frontier = {d.lower().replace("_", "-") for d in distributions}
    while frontier:
        dist = frontier.pop()
        if dist in seen:
            continue
        seen.add(dist)
        try:
            reqs = requires(dist) or []
        except PackageNotFoundError:
            continue
        for req in reqs:
            if ";" in req:
                continue
            match = _REQ_NAME.match(req)
            if match:
                frontier.add(match.group(1).lower().replace("_", "-"))
    return seen


def _import_names(distributions: set[str]) -> set[str]:
    """Import names the declared distributions -- and their dependencies -- provide."""
    by_dist: dict[str, set[str]] = {}
    for import_name, dists in packages_distributions().items():
        for dist in dists:
            by_dist.setdefault(dist.lower().replace("_", "-"), set()).add(import_name)
    names = set(REPO_PACKAGES)
    for dist in _dependency_closure(distributions):
        names.add(dist.replace("-", "_"))
        names |= by_dist.get(dist, set())
    return names


def _cases():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            declared = _declared_distributions(job)
            if declared is None:
                continue
            for module in sorted(_entry_modules(job)):
                yield pytest.param(
                    path.name, job_id, module, declared, id=f"{job_id}:{module}"
                )


CASES = list(_cases())


def test_the_case_list_is_not_empty():
    """A collection bug yielding nothing would make every case below vacuous --
    green because it checked nothing, the failure shape this file is about."""
    assert len(CASES) >= 8, [case.id for case in CASES]


@pytest.mark.parametrize(("workflow", "job_id", "module", "declared"), CASES)
def test_a_job_can_import_what_it_runs(workflow, job_id, module, declared) -> None:
    allowed = json.dumps(sorted(_import_names(declared)))
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, allowed, module],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{workflow}:{job_id} probe crashed for {module}:\n{result.stderr[-2000:]}"
    )
    missing = json.loads(result.stdout.splitlines()[-1])["missing"]
    assert missing is None, (
        f"{workflow}:{job_id} runs `python -m {module}`, which needs {missing!r} — "
        f"its `pip install {' '.join(sorted(declared))}` does not install it, so the "
        f"job dies at ModuleNotFoundError. A module gained a transitive import and "
        f"the hand-written list did not move with it (#783)."
    )
