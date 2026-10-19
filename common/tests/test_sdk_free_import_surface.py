"""Guard that every CI job without infra2-sdk runs only SDK-free modules (#1270).

`ops-checks.yml` installs the locked `infra2-sdk` wheel in all Python jobs (`audit`,
`watchdog`, `digest`, `deploy-v2-canary`, `preview-leak-check`,
`host-hygiene-schedule`, `facet-reconcile`, `vault-self-refresh-audit`,
`secrets-reconcile`, and `e2e-regressions-smoke` via `uv sync`). Only `pi-chain-smoke`
runs on a bare interpreter without `pip install`, executing `tools/pi_chain_smoke.py`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from common.tests.test_sdk_contract_adoption import _embedded_python

ROOT = Path(__file__).resolve().parent.parent.parent
OPS_CHECKS = ROOT / ".github/workflows/ops-checks.yml"

MUST_IMPORT_WITHOUT_SDK = [
    "tools.pi_chain_smoke",
    "common.security.registry",
]

SDK_BACKED_SECURITY_NAMES = [
    "SupplyReport",
    "apply_secret_supply",
    "create_secrets_resolver",
    "prune_orphan_secrets",
]

_BLOCKER = """
import sys
sys.path.insert(0, {root!r})
from importlib.abc import MetaPathFinder


class _Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "infra2_sdk" or name.startswith("infra2_sdk."):
            raise ImportError("No module named %r" % name)
        return None


sys.meta_path.insert(0, _Block())
__import__({module!r})
"""


@pytest.mark.parametrize("module", MUST_IMPORT_WITHOUT_SDK)
def test_module_imports_without_infra2_sdk(module: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _BLOCKER.format(module=module, root=str(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, (
        f"{module} failed to import without infra2-sdk.\n{proc.stderr[-800:]}"
    )


def test_the_list_is_not_empty() -> None:
    assert len(MUST_IMPORT_WITHOUT_SDK) >= 2


_INSTALLS_THE_SDK = re.compile(
    r"sdk_requirement|infra2[-_]sdk|\buv\s+(?:sync|pip\s+install)\b"
    r"|\bpip3?\s+install\b[^\n]*\s(?:-e\s+)?\.(?:\[[^\]]*\])?(?:\s|$)"
)
_PYTHON = r"\bpython3?(?:\s+-[A-Za-z]+)*"
_RUNS_MODULE = re.compile(_PYTHON + r"\s+-m\s+(?P<module>[A-Za-z_][\w.]*)")
_RUNS_SCRIPT = re.compile(_PYTHON + r"\s+(?P<path>(?:common|tools)/[\w/]+)\.py\b")
_IMPORTS_MODULE = re.compile(
    r"^[ \t]*(?:from|import)[ \t]+(?P<module>(?:common|tools)(?:\.\w+)*)", re.MULTILINE
)


def _sdk_free_job_modules(workflow: dict) -> dict[str, set[str]]:
    jobs: dict[str, set[str]] = {}
    for name, job in (workflow.get("jobs") or {}).items():
        runs = [
            step["run"]
            for step in job.get("steps") or []
            if isinstance(step, dict) and step.get("run")
        ]
        if any(_INSTALLS_THE_SDK.search(run) for run in runs):
            continue
        modules: set[str] = set()
        for run in runs:
            for match in _RUNS_MODULE.finditer(run):
                module = match["module"]
                if module == "invoke":
                    modules.add("tasks")
                elif module.split(".")[0] in ("common", "tools"):
                    modules.add(module)
            for match in _RUNS_SCRIPT.finditer(run):
                modules.add(match["path"].replace("/", "."))
            for snippet in _embedded_python(run):
                modules.update(m["module"] for m in _IMPORTS_MODULE.finditer(snippet))
        jobs[name] = modules
    return jobs


def test_the_job_to_module_derivation_follows_the_install_step() -> None:
    workflow = yaml.safe_load(
        """
jobs:
  bare:
    steps:
      - run: python -m pip install httpx rich
      - run: |
          python -m tools.alpha --flag
          python3 tools/beta.py
          python -m invoke deploy.audit
          python -m pytest
      - run: |
          python - <<'PY'
          from common.gamma.delta import thing
          import tools.epsilon
          import json
          PY
  with_the_sdk:
    steps:
      - run: |
          requirement="$(python tools/sdk_requirement.py)"
          python -m pip install "$requirement"
      - run: python -m tools.zeta
  project_install:
    steps:
      - run: python -m pip install -e .[dev]
      - run: python -m tools.eta
"""
    )
    assert _sdk_free_job_modules(workflow) == {
        "bare": {
            "tools.alpha",
            "tools.beta",
            "tasks",
            "common.gamma.delta",
            "tools.epsilon",
        }
    }


def test_the_list_covers_every_module_the_sdk_free_ops_checks_jobs_run() -> None:
    jobs = _sdk_free_job_modules(yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8")))
    assert set(jobs) == {"pi-chain-smoke"}, f"unexpected SDK-free jobs: {jobs}"
    assert jobs["pi-chain-smoke"] == {"tools.pi_chain_smoke"}
    missing = {
        job: sorted(modules - set(MUST_IMPORT_WITHOUT_SDK))
        for job, modules in jobs.items()
        if modules - set(MUST_IMPORT_WITHOUT_SDK)
    }
    assert missing == {}, f"add these to MUST_IMPORT_WITHOUT_SDK: {missing}"


def test_a_module_that_genuinely_needs_the_sdk_still_fails() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _BLOCKER.format(module="common.security.supply", root=str(ROOT)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0, (
        "common.security.supply imported with SDK blocked — blocker is broken"
    )


def test_security_lazy_exports_match_the_public_surface_with_the_sdk() -> None:
    import common.security as security
    from common.security import prune, store, supply

    owners = {
        "SupplyReport": supply,
        "apply_secret_supply": supply,
        "create_secrets_resolver": supply,
        "prune_orphan_secrets": prune,
        "VaultSecrets": store,
        "generate_secret_token": store,
        "resolve_vault_token": store,
    }
    assert sorted(owners) == sorted(security.__all__)
    for name, owner in owners.items():
        assert getattr(security, name) is getattr(owner, name), name
    with pytest.raises(AttributeError, match="no attribute 'not_a_name'"):
        security.not_a_name  # noqa: B018
    assert set(security.__all__) <= set(dir(security))
