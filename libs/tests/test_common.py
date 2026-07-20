"""Tests for libs/common.py's shared, domain-independent helpers."""

from __future__ import annotations

from libs.common import infra_domain


def test_infra_domain_reads_the_environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_DOMAIN", "example.internal")
    assert infra_domain() == "example.internal"


def test_infra_domain_defaults_to_zitian_party_when_unset(monkeypatch):
    monkeypatch.delenv("INTERNAL_DOMAIN", raising=False)
    assert infra_domain() == "zitian.party"


def test_infra_domain_strips_whitespace(monkeypatch):
    # A stray newline/space in the env var would otherwise build an invalid host
    # (e.g. "vault.zitian.party\n") — strip defensively, matching every other
    # INTERNAL_DOMAIN reader in the repo (tools/signoz_alert_rule_canary.py).
    monkeypatch.setenv("INTERNAL_DOMAIN", "  zitian.party\n")
    assert infra_domain() == "zitian.party"


def test_infra_domain_falls_back_when_only_whitespace(monkeypatch):
    monkeypatch.setenv("INTERNAL_DOMAIN", "   ")
    assert infra_domain() == "zitian.party"


def test_infra_domain_takes_no_caller_supplied_fallback():
    # No fallback parameter exists on purpose: accepting one would let a caller pass its
    # own app-domain override back in, reintroducing the exact conflation this closes
    # (a truealpha/app deploy resolving the shared Dokploy/Vault/SigNoz host against
    # truealpha.club instead of the real zitian.party control plane).
    import inspect

    assert list(inspect.signature(infra_domain).parameters) == []
