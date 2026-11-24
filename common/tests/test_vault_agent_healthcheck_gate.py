"""The `validate-vault-agent` CI gate (.github/workflows/infra-ci.yml), mirrored so it
cannot silently skip every file again.

The gate selected composes with `grep -q VAULT_APP_TOKEN`. No compose has carried that
variable since the AppRole migration (#369), so from then on the gate's four checks ran
over zero files and the job stayed green — while truealpha/data_engine's vault-agent
healthcheck lacked the token-lookup marker the other 15 carry (#713, batch D item 15).
The gate now selects by the sidecar's service key; this test pins that marker to the
live fleet and re-runs the four checks, plus the probe itself, parsed from YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/infra-ci.yml"
COMPOSE_ROOTS = ("bootstrap", "platform", "finance_report", "truealpha")
GATE_MARKER = "\n  vault-agent:\n"  # what `grep -q "^  vault-agent:"` matches


def _vault_agent_composes() -> list[str]:
    found = []
    for top in COMPOSE_ROOTS:
        for compose in sorted((ROOT / top).rglob("compose.yaml")):
            if GATE_MARKER in compose.read_text(encoding="utf-8"):
                found.append(str(compose.relative_to(ROOT)))
    return found


VAULT_AGENT_COMPOSE_FILES = _vault_agent_composes()


def test_gate_selects_files_by_a_marker_the_fleet_still_carries():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'grep -q "^  vault-agent:" "$file"' in workflow, (
        "validate-vault-agent no longer selects composes by the vault-agent service key"
    )
    assert 'grep -q "VAULT_APP_TOKEN" "$file"' not in workflow, (
        "VAULT_APP_TOKEN left every compose with #369; a gate keyed on it checks nothing"
    )
    # 17 sidecars on 2026-11-24: 13 hand-written (the shell gate) and 4 compiled (#1822).
    sidecars = VAULT_AGENT_COMPOSE_FILES + _x_infra2_vault_composes()
    assert len(sidecars) >= 17, sidecars
    assert (
        "truealpha/truealpha/20.data_engine/compose.yaml" in VAULT_AGENT_COMPOSE_FILES
    )
    stale = [
        path
        for path in VAULT_AGENT_COMPOSE_FILES
        if "VAULT_APP_TOKEN" in (ROOT / path).read_text(encoding="utf-8")
    ]
    assert not stale, (
        f"static-token marker is back in {stale}; the gate assumes AppRole"
    )


@pytest.mark.parametrize("relative_path", VAULT_AGENT_COMPOSE_FILES)
def test_vault_agent_healthcheck_passes_the_gate(relative_path):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    # The gate's four literal checks, verbatim.
    assert "rm -f /vault/secrets/.env" in text, (
        f"{relative_path}: vault-agent does not clear stale secrets before auth"
    )
    assert "vault token lookup" in text, (
        f"{relative_path}: vault-agent healthcheck does not validate token lookup"
    )
    assert "<no value>" in text, (
        f"{relative_path}: healthcheck does not reject unresolved template values"
    )
    assert (
        "VAULT_AGENT_MAX_SECRET_AGE_SECONDS" not in text and "stat -c %Y" not in text
    ), f"{relative_path}: healthcheck must not use rendered-file mtime freshness"
    # Stronger than the literals: the two-stage probe itself, parsed from the compose.
    agent = yaml.safe_load(text)["services"]["vault-agent"]
    test = agent["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL", f"{relative_path}: probe is not a shell script"
    probe = " ".join(test[1:])
    assert "/vault/secrets/.env" in probe and "/vault/.token" in probe, (
        f"{relative_path}: stage 1 (local: rendered secrets + token file) missing"
    )
    assert "/v1/auth/token/lookup-self" in probe and "/tmp/.vault_hc" in probe, (
        f"{relative_path}: stage 2 (throttled Vault token lookup, #292) missing"
    )


def _x_infra2_vault_composes() -> list[str]:
    """x-infra2 stacks with a compiled vault-agent. The shell gate cannot see them: the
    source has no `  vault-agent:` key (#1822 Slice 2)."""
    from common.deploy.compose_compile import compiles_a_vault_sidecar, has_x_infra2

    found = []
    for top in COMPOSE_ROOTS:
        for compose in sorted((ROOT / top).rglob("compose.yaml")):
            text = compose.read_text(encoding="utf-8")
            if has_x_infra2(text) and compiles_a_vault_sidecar(text):
                assert GATE_MARKER not in text
                found.append(str(compose.relative_to(ROOT)))
    return found


X_INFRA2_VAULT_COMPOSE_FILES = _x_infra2_vault_composes()


def test_the_x_infra2_todo_is_checked_here_and_not_by_the_shell_gate():
    assert "platform/30.todo/compose.yaml" in X_INFRA2_VAULT_COMPOSE_FILES
    assert not set(X_INFRA2_VAULT_COMPOSE_FILES) & set(VAULT_AGENT_COMPOSE_FILES)


@pytest.mark.parametrize("relative_path", X_INFRA2_VAULT_COMPOSE_FILES)
def test_a_compiled_vault_agent_healthcheck_is_two_stage(relative_path):
    from common.deploy.compose_compile import load_stack

    agent = yaml.safe_load(load_stack(relative_path).compiled)["services"][
        "vault-agent"
    ]
    assert "rm -f /vault/secrets/.env" in agent["entrypoint"][2]
    test = agent["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL"
    probe = " ".join(test[1:])
    assert "<no value>" in probe
    assert "/vault/secrets/.env" in probe and "/vault/.token" in probe
    assert "/v1/auth/token/lookup-self" in probe and "/tmp/.vault_hc" in probe
    assert "VAULT_AGENT_MAX_SECRET_AGE_SECONDS" not in probe
    assert "stat -c %Y" not in probe
