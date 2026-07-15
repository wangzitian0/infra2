"""AppRole migration invariants — regression guards (#257/#259/#369).

The Vault `token_file`→AppRole migration is complete. These lock the invariants in CODE so a
later change can't silently regress them:

  1. Every vault-agent authenticates via AppRole — the `token_file` model stays retired.
  2. The vault-self-refresh inventory (SSOT) agrees — every service is `approle`.
  3. Every vault-agent compose entrypoint FAILS FAST (``exit 1``) on a missing required cred
     (``VAULT_ROLE_ID`` / ``VAULT_SECRET_ID`` / ``VAULT_ADDR``) instead of silently deadlocking
     ~6 min on its healthcheck — the exact gap that left iac_runner exposed until the entrypoint
     guard was extended to it.

The service set is DERIVED from the filesystem + the audit's own discovery (never a hardcoded
list), so a newly-added vault-agent service is automatically held to the same invariants.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from libs.vault_self_refresh_audit import (
    REPO_ROOT,
    discover_vault_agent_compose_paths,
    load_inventory,
)

VAULT_AGENT_HCLS = sorted(
    path
    for path in REPO_ROOT.rglob("vault-agent.hcl")
    if path.relative_to(REPO_ROOT).parts[0] != "repos"
)
VAULT_AGENT_COMPOSES = sorted(discover_vault_agent_compose_paths())


def test_discovery_is_non_empty() -> None:
    """Sanity: if discovery returned nothing the parametrized guards below would vacuously
    pass, hiding a regression. Pin a sane floor."""
    assert len(VAULT_AGENT_HCLS) >= 12, VAULT_AGENT_HCLS
    assert len(VAULT_AGENT_COMPOSES) >= 12, VAULT_AGENT_COMPOSES


@pytest.mark.parametrize(
    "hcl", VAULT_AGENT_HCLS, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_vault_agent_uses_approle_not_token_file(hcl: Path) -> None:
    """Drift guard: the `token_file` model was retired in #369. Every vault-agent must
    auto_auth via AppRole, and `type = "token_file"` must never come back."""
    text = hcl.read_text(encoding="utf-8")
    # match the auth-method config, not a stray mention in a migration comment. Strip ALL
    # whitespace (spaces/tabs/newlines) so a reformatted `type =\n"token_file"` can't slip past.
    normalized = re.sub(r"\s+", "", text)
    assert 'type="token_file"' not in normalized and 'method"token_file"' not in normalized, (
        f"{hcl.relative_to(REPO_ROOT)} reintroduced token_file auth — retired in #369; "
        "use AppRole (role_id + secret_id)."
    )
    assert 'method "approle"' in text, (
        f'{hcl.relative_to(REPO_ROOT)} must use auto_auth `method "approle"`.'
    )


def test_inventory_is_entirely_approle() -> None:
    """The vault-self-refresh SSOT must mark every service `approle` — no `token_file` rows."""
    non_approle = sorted(s.id for s in load_inventory() if s.auth_method != "approle")
    assert not non_approle, (
        f"vault-self-refresh-inventory.yaml has non-approle rows: {non_approle}. "
        "Every service migrated to AppRole (#369)."
    )


# The required-cred entrypoint guards, by env var → the exact shell test that must be present.
_REQUIRED_GUARDS = {
    "VAULT_ROLE_ID": '[ -z "$$VAULT_ROLE_ID" ]',
    "VAULT_SECRET_ID": '[ -z "$$VAULT_SECRET_ID" ]',
    "VAULT_ADDR": '[ -z "$$VAULT_ADDR" ]',
}


@pytest.mark.parametrize("compose", VAULT_AGENT_COMPOSES, ids=lambda p: str(p))
def test_vault_agent_entrypoint_fails_fast_on_missing_creds(compose: str) -> None:
    """Every vault-agent compose entrypoint must EXIT on an empty required cred, not hang.

    The compose declares ``VAULT_ADDR: ${VAULT_ADDR}`` (and the AppRole creds) with no default,
    so without these guards a missing value flows into the vault-agent, which then hangs
    reaching an empty address / failing to log in — and the dependent service deadlocks on
    ``depends_on: vault-agent: service_healthy`` for the full ~6-min healthcheck window with no
    actionable error. Assert each guard's *check* is present (not just its message)."""
    text = (REPO_ROOT / compose).read_text(encoding="utf-8")
    missing = [name for name, check in _REQUIRED_GUARDS.items() if check not in text]
    assert not missing, (
        f"{compose} vault-agent entrypoint is missing a fail-fast guard for {missing} — a "
        "missing value would deadlock the healthcheck instead of erroring fast."
    )
    # Each guard must ABORT, not merely warn: assert `exit 1` lives inside the guard's own
    # block (check → its closing `fi`), so an unrelated `exit 1` elsewhere can't mask a
    # guard that stopped exiting.
    for name, check in _REQUIRED_GUARDS.items():
        idx = text.find(check)
        closing = re.search(r"\bfi\b", text[idx:])
        block = text[idx : idx + (closing.end() if closing else 200)]
        assert "exit 1" in block, (
            f"{compose}: the {name} fail-fast guard is present but its block does not "
            "`exit 1` — it would not abort the container on a missing value."
        )
