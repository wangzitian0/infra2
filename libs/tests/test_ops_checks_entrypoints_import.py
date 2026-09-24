"""#923: every ops-checks job's entrypoints import with only that job's packages.

The in-process suite hides import-time failures: pytest runs with every dev
dependency installed and the stdlib already loaded. Twice this bit for real --
the rehearsal cron crashed on the repo's `platform/` package shadowing the
stdlib (#892), and a module-level PyYAML import would have taken down the
watchdog job, which installs only httpx, python-dotenv and rich (#895). So each
job's entrypoints are imported in a fresh interpreter, with the repo root ahead
of the stdlib as the job has it, and every site-packages module outside the
job's `pip install` closure blocked.
"""

from __future__ import annotations

import importlib.metadata as metadata
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ops-checks.yml"
#: Shell tokens the workflow uses for the SDK pin (read from pyproject.toml).
SDK_TOKENS = ("$sdk_requirement", "infra2-sdk @")


def _job_packages(runs: list[str]) -> set[str] | None:
    """The distributions a job's `pip install` lines name; None when it has none."""
    names: set[str] = set()
    found = False
    for line in (line for run in runs for line in run.splitlines()):
        if "pip install" not in line:
            continue
        found = True
        if any(token in line for token in SDK_TOKENS):
            names.add("infra2-sdk")
        tail = line.split("pip install", 1)[1]
        for word in tail.split():
            word = word.strip("\"'")
            if word and not word.startswith(("-", "$", "(", "@")) and "/" not in word:
                names.add(word)
    return names if found else None


def _entrypoints(runs: list[str]) -> set[str]:
    text = "\n".join(runs)
    scripts = re.findall(r"python3? (?:-m )?((?:tools|libs)[./][\w./]+)", text)
    imports = re.findall(r"from ((?:tools|libs)\.[\w.]+) import", text)
    return {re.sub(r"\.py$", "", name).replace("/", ".") for name in scripts + imports}


def _closure(names: set[str]) -> set[str]:
    """The distributions `pip install <names>` pulls in, from this venv's metadata."""
    seen: set[str] = set()
    pending = [name.lower() for name in names]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for raw in requires:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            pending.append(requirement.name.lower())
    return seen


def _top_level_modules(distributions: set[str]) -> set[str]:
    wanted = {name.replace("_", "-") for name in distributions}
    return {
        module
        for module, dists in metadata.packages_distributions().items()
        if any(dist.lower().replace("_", "-") in wanted for dist in dists)
    }


def _jobs() -> list[tuple[str, set[str], set[str]]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = []
    for name, job in workflow["jobs"].items():
        runs = [step.get("run", "") for step in job.get("steps", [])]
        packages = _job_packages(runs)
        entries = _entrypoints(runs)
        if packages is not None and entries:
            jobs.append((name, packages, entries))
    return jobs


_BLOCKER = """
import importlib, importlib.machinery, sys
ALLOWED = set(sys.argv[1].split(","))
class JobOnly:
    def find_spec(self, name, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path, target)
        if spec and "-packages" in (spec.origin or "") and name.split(".")[0] not in ALLOWED:
            raise ImportError(name + " is not installed in this ops-checks job")
        return None
sys.meta_path.insert(0, JobOnly())
failed = []
for module in sys.argv[2].split(","):
    try:
        importlib.import_module(module)
    except BaseException as exc:
        failed.append(f"{module}: {type(exc).__name__}: {exc}")
print("\\n".join(failed))
sys.exit(1 if failed else 0)
"""


def test_the_workflow_declares_jobs_to_check() -> None:
    """Parsing must find the jobs, or the parametrized test below runs nothing."""
    entries = {name: entry for name, _, entry in _jobs()}

    assert {"watchdog", "digest", "facet-reconcile", "preview-leak-check"} <= set(
        entries
    )
    assert "tools.stability_report" in entries["digest"]
    assert "tools.out_of_band_watchdog" in entries["watchdog"]


@pytest.mark.parametrize(
    ("job", "packages", "entries"), _jobs(), ids=[name for name, _, _ in _jobs()]
)
def test_job_entrypoints_import_with_only_the_jobs_packages(
    job: str, packages: set[str], entries: set[str]
) -> None:
    allowed = _top_level_modules(_closure(packages))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _BLOCKER,
            ",".join(sorted(allowed)),
            ",".join(sorted(entries)),
        ],
        cwd=ROOT,
        env={**env, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{job}: {result.stdout}{result.stderr}"
