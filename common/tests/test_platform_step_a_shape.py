"""The hand platform stacks with a Vault sidecar have the compiled x-infra2 shape (#1822 step A).

Each file stays plain. Step B swaps it to an x-infra2 source. This test does a dry run
of step B: it removes the hand `vault-agent`, adds `x-infra2.vault.name` (the old
container prefix) and `x-vault: {}` on each service that runs the SDK wait entrypoint,
compiles the draft with the deploy renderer, and compares it with the hand file by
`equivalence_problems`, the check that step B must pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from common.deploy.compose_compile import compile_stack, vault_template_for
from common.deploy.compose_equivalence import equivalence_problems

ROOT = Path(__file__).resolve().parents[2]
# platform/02.redis, platform/01.postgres, platform/23.prefect, platform/03.s3 and
# platform/10.authentik left this list in #1822 step B: each file is an x-infra2 source
# now, and `tools.compose_compile check` proves it against the step-A file.
STACKS = (
    "platform/12.alerting",
    "platform/24.openpanel",
)
SUFFIX = "-vault-agent${ENV_SUFFIX}"
#: Each service that reads /secrets/.env without the SDK wait entrypoint.
NOT_X_VAULT = {"platform/12.alerting": {"infra-probe-runner"}}


def _sdk_wait_entrypoint() -> list[str]:
    source = (
        "x-infra2:\n  vault:\n    name: probe\n"
        "services:\n  app:\n    image: a:1\n    command: [run]\n    x-vault: {}\n"
    )
    compiled = yaml.safe_load(compile_stack(source, vault_template="X=1\n"))
    return compiled["services"]["app"]["entrypoint"]


def draft_step_b(hand_text: str) -> tuple[str, list[str]]:
    """The x-infra2 source of step B, and the services that it marks `x-vault`."""
    doc = yaml.safe_load(hand_text)
    sidecar = doc["services"].pop("vault-agent")
    name = sidecar["container_name"]
    assert name.endswith(SUFFIX), name
    wait = _sdk_wait_entrypoint()
    marked = []
    for svc, spec in doc["services"].items():
        if spec.get("entrypoint") == wait:
            spec.pop("entrypoint")
            spec["x-vault"] = {}
            marked.append(svc)
    source = {"x-infra2": {"vault": {"name": name[: -len(SUFFIX)]}}, **doc}
    return yaml.safe_dump(source, sort_keys=False, width=10**6), marked


def _base_files(stack: str) -> dict[str, str | None]:
    return {
        n: (ROOT / stack / n).read_text(encoding="utf-8")
        for n in ("secrets.ctmpl", "vault-agent.hcl")
    }


@pytest.mark.parametrize("stack", STACKS)
def test_a_step_b_dry_run_has_no_problem(stack: str) -> None:
    path = f"{stack}/compose.yaml"
    hand = (ROOT / path).read_text(encoding="utf-8")
    source, marked = draft_step_b(hand)
    readers = {
        svc
        for svc, spec in yaml.safe_load(hand)["services"].items()
        if svc != "vault-agent" and "secrets:/secrets:ro" in (spec.get("volumes") or [])
    }
    assert set(marked) == readers - NOT_X_VAULT.get(stack, set())
    compiled = compile_stack(source, vault_template=vault_template_for(path, source))
    assert equivalence_problems(source, compiled, hand, _base_files(stack)) == []


def test_the_dry_run_finds_a_hand_sidecar_value() -> None:
    # The sample is a step-A stack. platform/01.postgres, platform/03.s3 and
    # platform/10.authentik moved to x-infra2 (#1822 step B) and have no hand sidecar
    # now, so the sample is platform/24.openpanel.
    hand = (ROOT / "platform/24.openpanel/compose.yaml").read_text(encoding="utf-8")
    assert hand.count("      retries: 12\n") == 1
    changed = hand.replace("      retries: 12\n", "      retries: 3\n")
    source, _ = draft_step_b(changed)
    path = "platform/24.openpanel/compose.yaml"
    compiled = compile_stack(source, vault_template=vault_template_for(path, source))
    assert equivalence_problems(
        source, compiled, changed, _base_files("platform/24.openpanel")
    ) == ["services.vault-agent.healthcheck.retries: compiled 12 != base 3"]
