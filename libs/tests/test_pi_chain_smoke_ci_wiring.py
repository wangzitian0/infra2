"""The CI step must let pi_chain_smoke.py --strict decide, and it did not.

``tools/pi_chain_smoke.py`` already encodes the policy: ``--strict`` exists so
that a missing credential becomes exit 2 (infra error) instead of a local SKIP.
Its own docstring says so -- ``--strict  # CI: missing creds -> exit 2``.

The only caller passed ``--strict`` and then made it unreachable: a shell
branch ahead of it printed ``SKIP`` and ``exit 0`` whenever the secret was
empty. Every run without the secret would therefore have reported success
without ever launching pi -- and on 2026-09-22, when this was written,
``ZAI_CODING_CN_API_KEY`` was not among the repository's secrets, so that was
every run the daily "real token-consuming E2E proof of the pi main chain"
would ever have made. Exactly the failure the tool was written to stop, since
#758's premise is that "every unit/fixture gate stayed green while the chain
was broken in production".

Whether the secret is configured today is not what this test depends on. The
defect was the step answering the credential question at all; the assertions
below hold either way.

**This test executes the step rather than reading it.** Asserting on the shell
text would only forbid the one spelling that happened to be used: a guard that
matches command text cannot enumerate the ways a script can reach exit 0 --
``|| true``, ``set +e``, a ``case``, a helper function, an early ``return``.
The physical property is narrower and closed: *run the step with no credential
and a tool that refuses, and the step must refuse too.* That holds regardless
of how the script is written, so a future rewrite cannot re-open the hole
without failing here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ops-checks.yml"
JOB = "pi-chain-smoke"

# What the real tool returns when it refuses: 1 = the chain assertions failed,
# 2 = infra error, which is what --strict turns a missing credential into.
REFUSAL_EXITS = (1, 2)


def _smoke_step() -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    steps = [s for s in jobs[JOB]["steps"] if "pi_chain_smoke" in (s.get("run") or "")]
    assert len(steps) == 1, f"expected exactly one step invoking the smoke, got {steps}"
    return steps[0]


def test_the_step_hands_the_verdict_to_the_tool_and_passes_strict() -> None:
    """--strict must be on, and the secret must actually reach the process."""
    step = _smoke_step()
    assert "--strict" in step["run"]
    assert (
        step["env"]["ZAI_CODING_CN_API_KEY"] == "${{ secrets.ZAI_CODING_CN_API_KEY }}"
    )


@pytest.mark.parametrize("tool_exit", REFUSAL_EXITS)
def test_a_refusing_tool_fails_the_step_with_no_credential(
    tmp_path: Path, tool_exit: int
) -> None:
    """Run the step's own script; a refusal must not become a green job.

    The secret is exported empty, which is what GitHub injects for a secret the
    repository does not define -- the condition every scheduled run has met so
    far.
    """
    step = _smoke_step()
    run = step["run"]
    assert "${{" not in run, (
        "the step body interpolates a workflow expression, so this test would "
        "execute something other than what CI executes; expand the harness "
        "before letting that through"
    )

    # Stand in for the tool: record that it was reached, then refuse.
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "pi_chain_smoke.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path('reached.json').write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.exit({tool_exit})\n",
        encoding="utf-8",
    )
    script = tmp_path / "step.sh"
    script.write_text(run, encoding="utf-8")

    # Inherit the environment and override only what the scenario needs. A
    # hand-picked PATH-and-HOME one is not a smaller version of a runner's
    # environment but a different one: with HOME moved, a python3 resolved
    # through a version manager's shim fails to start (exit 126), and this
    # test then reports "the step never invoked the smoke tool" -- which reads
    # exactly like the wiring defect it exists to catch. The sibling test in
    # test_docs_only_prs_run_their_own_gates.py takes the same position; the
    # two are deliberately consistent.
    env = dict(os.environ)
    # An undefined repository secret arrives as the empty string.
    env["ZAI_CODING_CN_API_KEY"] = ""
    proc = subprocess.run(
        # GitHub's default shell for `run:` on Linux is `bash -e {0}`.
        ["bash", "-e", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        # No step here should take seconds. Unbounded, a blocked subprocess
        # holds a required check until GitHub's 6-hour job default.
        timeout=120,
        env=env,
    )

    reached = tmp_path / "reached.json"
    assert reached.is_file(), (
        "the step returned without ever invoking the smoke tool "
        f"(exit={proc.returncode}, stdout={proc.stdout.strip()!r}) -- it decided "
        "the outcome itself, so --strict never got to"
    )
    assert "--strict" in json.loads(reached.read_text(encoding="utf-8"))
    assert proc.returncode != 0, (
        f"the tool refused with exit {tool_exit} and the step still reported "
        f"success (stdout={proc.stdout.strip()!r})"
    )
