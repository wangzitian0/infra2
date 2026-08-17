"""Tests for libs/common.py's shared, domain-independent helpers."""

from __future__ import annotations

from libs import service_registry
from libs.common import (
    _BOOTSTRAP_ONLY_SHARED_SERVICES,
    _REGISTRY_BACKED_SHORT_NAMES,
    SHARED_PLATFORM_SERVICES,
    infra_domain,
    check_service,
)


def test_infra_domain_reads_the_environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_DOMAIN", "example.internal")
    assert infra_domain() == "example.internal"


def test_infra_domain_defaults_to_zitian_party_when_unset(monkeypatch):
    monkeypatch.delenv("INTERNAL_DOMAIN", raising=False)
    assert infra_domain() == "zitian.party"


def test_infra_domain_strips_whitespace(monkeypatch):
    # A stray newline/space in the env var would otherwise build an invalid host
    # (e.g. "vault.zitian.party\n") — strip defensively, matching every other
    # INTERNAL_DOMAIN reader in the repo (tools/signoz_alert_rule_probe.py).
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


# --- SHARED_PLATFORM_SERVICES: derived, not hand-maintained (infra2#596) ---------


def test_bootstrap_only_shared_services_stays_exactly_vault_and_dokploy():
    # These two have no service_registry entry at all (no deploy.py — see infra2#596)
    # so they can never be derived; this is the one deliberately-hardcoded exception.
    # A change either direction must touch this test, not drift silently.
    assert _BOOTSTRAP_ONLY_SHARED_SERVICES == frozenset({"vault", "dokploy"})


def test_every_registry_backed_short_name_maps_to_a_real_registered_service():
    # If a short name here is renamed/retired in its own deploy.py, this fails loudly
    # instead of _derive_shared_platform_services() silently treating it as unshared.
    attrs = service_registry.service_attrs()
    for short_name, key in _REGISTRY_BACKED_SHORT_NAMES.items():
        assert key in attrs, f"{short_name!r} maps to {key!r}, not in service_attrs()"


def test_shared_platform_services_registry_portion_matches_prod_only():
    # The whole point of deriving instead of hand-maintaining: every member beyond the
    # bootstrap-plane pair must correspond to a REAL prod_only=True service. This is
    # exactly the assertion that would have caught sso/minio_api/minio_console being
    # wrongly hardcoded in as "shared" when authentik/minio are actually prod_only=False
    # (real, separate staging instances exist: sso-staging.zitian.party,
    # s3-staging.zitian.party).
    attrs = service_registry.service_attrs()
    shared = SHARED_PLATFORM_SERVICES()
    registry_portion = shared - _BOOTSTRAP_ONLY_SHARED_SERVICES
    for short_name in registry_portion:
        key = _REGISTRY_BACKED_SHORT_NAMES[short_name]
        assert attrs[key].prod_only is True, (
            f"{short_name!r} ({key!r}) is in SHARED_PLATFORM_SERVICES() but "
            f"prod_only={attrs[key].prod_only!r} — it has a real per-env instance "
            "and must not be treated as one shared endpoint."
        )


def test_shared_platform_services_excludes_a_registry_backed_name_whose_service_is_not_prod_only():
    # authentik (short name "sso") is prod_only=False today — a live, separate
    # staging instance actually exists. Pin this so a future prod_only flip on
    # authentik is what changes this test, not a silent hand-edit of a hardcoded set.
    attrs = service_registry.service_attrs()
    assert attrs["platform/authentik"].prod_only is False
    assert "sso" not in SHARED_PLATFORM_SERVICES()


def test_check_service_quotes_nested_health_command(monkeypatch):
    class _Result:
        ok = True

    class _Context:
        command = ""

        def run(self, command, **_kwargs):
            self.command = command
            return _Result()

    monkeypatch.setattr(
        "libs.common.get_env",
        lambda: {"VPS_HOST": "host.example", "ENV": "production"},
    )
    context = _Context()
    health = (
        'python -c "import urllib.request; '
        "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)\""
    )

    result = check_service(context, "truealpha-llm", health)

    assert result["is_ready"] is True
    assert "root@host.example" in context.command
    assert "docker exec truealpha-llm sh -lc" in context.command
    assert "http://127.0.0.1:8000/health" in context.command
