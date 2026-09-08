"""Every Vault Agent policy lets the agent renew and look up its own token.

The AppRoles are created with token_no_default_policy=true (bootstrap/05.vault/tasks.py),
so the two grants the `default` policy would carry come from the service policy. Vault
Agent renews through auth/token/renew-self regardless (the LifetimeWatcher renews at once,
then ahead of every TTL); 1.15 retries a 403 there without backoff. 2026-09-08: the six
sidecars whose policies had been (re)written after #369 each issued ~35 renew-self/s
through the public Vault route — 0.5-1.9 cores of Traefik and 0.3 of Vault, for weeks.
The other 20 sidecars were still on live policies written before #369 while every
repository file lacked the grant (this test was red on all 16 when it was written), so the
invariant is kept here rather than on the host: a policy file without the grant is one
`setup-approle` away from the same loop.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICIES = sorted(ROOT.glob("**/vault-policy.hcl"))


def _grants(text: str, path: str) -> set[str]:
    match = re.search(rf'path "{re.escape(path)}"\s*{{\s*capabilities\s*=\s*\[([^\]]*)\]', text)
    return set(re.findall(r'"([a-z]+)"', match.group(1))) if match else set()


def test_every_policy_is_found() -> None:
    assert len(POLICIES) >= 16, [str(p.relative_to(ROOT)) for p in POLICIES]


@pytest.mark.parametrize("policy", POLICIES, ids=lambda p: str(p.relative_to(ROOT)))
def test_agent_policy_lets_the_agent_renew_and_look_up_its_own_token(policy: Path) -> None:
    text = policy.read_text(encoding="utf-8")
    assert "update" in _grants(text, "auth/token/renew-self"), f"{policy}: no renew-self grant"
    assert "read" in _grants(text, "auth/token/lookup-self"), f"{policy}: no lookup-self grant"
