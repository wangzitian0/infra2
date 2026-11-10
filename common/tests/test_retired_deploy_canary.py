"""The deploy_v2 canary stays retired (#1477). The Canary Todo is the one Canary.

No workflow may start ``tools.deploy_v2_canary`` or name a retired canary job:
``preflight_canary`` (deploy.yml, app-deploy-request.yml) or ``deploy-v2-canary``
(ops-checks.yml). The scan reads job ids from the parsed YAML and every other
reference (``needs``, a task option, a concurrency group, a path) from the text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.y*ml"))

RETIRED_JOBS = frozenset({"preflight_canary", "deploy-v2-canary"})
RETIRED_TEXT = re.compile(
    r"tools[./]deploy_v2_canary\b|\bpreflight_canary\b|\bdeploy-v2-canary\b"
)


def retired_references(name: str, text: str) -> list[str]:
    """Each reference to the retired canary in one workflow file."""
    found = [
        f"{name}:{text.count(chr(10), 0, match.start()) + 1}: {match.group(0)}"
        for match in RETIRED_TEXT.finditer(text)
    ]
    document = yaml.safe_load(text) or {}
    for job in document.get("jobs") or {}:
        if job in RETIRED_JOBS:
            found.append(f"{name}: declares the retired job {job}")
    return found


def test_no_workflow_references_the_retired_deploy_canary() -> None:
    names = {path.name for path in WORKFLOWS}
    # The scan must read the files that held the canary, or it passes on nothing.
    assert {"deploy.yml", "ops-checks.yml"} <= names
    assert "app-deploy-request.yml" not in names
    assert "deploy-report-main.yml" not in names
    assert "preview-teardown.yml" not in names
    found = [
        reference
        for path in WORKFLOWS
        for reference in retired_references(path.name, path.read_text(encoding="utf-8"))
    ]
    assert found == []
    assert not (ROOT / "tools" / "deploy_v2_canary.py").exists()


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  preflight_canary:\n    runs-on: ubuntu-latest\n",
        "jobs:\n  deploy-v2-canary:\n    runs-on: ubuntu-latest\n",
        "jobs:\n  deploy:\n    needs: [preflight_canary]\n",
        "jobs:\n  j:\n    steps:\n      - run: python -m tools.deploy_v2_canary --domain x\n",
        'on:\n  push:\n    paths: ["tools/deploy_v2_canary.py"]\njobs: {}\n',
        "jobs:\n  j:\n    concurrency:\n      group: deploy-v2-canary\n",
    ],
    ids=["job-id", "ops-job-id", "needs", "run", "path", "group"],
)
def test_each_form_of_a_retired_reference_is_found(text: str) -> None:
    assert retired_references("w.yml", text) != []


def test_a_name_that_only_contains_a_retired_word_is_not_found() -> None:
    text = "jobs:\n  todo-canary-status:\n    steps:\n      - run: tools/deploy_v2.py\n"
    assert retired_references("w.yml", text) == []
