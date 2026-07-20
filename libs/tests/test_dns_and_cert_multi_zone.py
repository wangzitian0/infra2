"""Tests for bootstrap/02.dns_and_cert's --domain zone-override (multi-zone DNS apply).

A per-app custom domain (truealpha.club, #550) is a DIFFERENT Cloudflare zone than the
shared INTERNAL_DOMAIN — CF_ZONE_ID must never be reused across zones, and records must
normalize against the override domain, not INTERNAL_DOMAIN. No live Cloudflare call: the
client, secrets, and env are all monkeypatched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "dns_and_cert_tasks", ROOT / "bootstrap/02.dns_and_cert/tasks.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def m():
    return _mod()


def _stub_env(m, monkeypatch, secrets):
    monkeypatch.setattr(
        m, "get_env", lambda: {"INTERNAL_DOMAIN": "zitian.party", "VPS_HOST": "1.2.3.4"}
    )
    monkeypatch.setattr(m, "_load_cloudflare_secrets", lambda: secrets)


def test_default_zone_reuses_the_pinned_zone_id(m, monkeypatch):
    _stub_env(m, monkeypatch, {"CF_API_TOKEN": "tok", "CF_ZONE_ID": "zid-shared"})
    seen = {}

    def fake_resolve_zone_id(client, zone_id, zone_name):
        seen["zone_id"] = zone_id
        seen["zone_name"] = zone_name
        return "resolved"

    monkeypatch.setattr(m, "_resolve_zone_id", fake_resolve_zone_id)
    monkeypatch.setattr(m, "_ensure_record", lambda *a, **k: True)
    monkeypatch.setattr(m, "_cloudflare_client", lambda token: _FakeClientCtx())

    assert m._ensure_dns_records(["cloud"], True, 1) is True
    # no --domain override -> the pinned CF_ZONE_ID is reused, zone name = INTERNAL_DOMAIN
    assert seen == {"zone_id": "zid-shared", "zone_name": "zitian.party"}


def test_domain_override_never_reuses_the_default_zone_id(m, monkeypatch):
    _stub_env(m, monkeypatch, {"CF_API_TOKEN": "tok", "CF_ZONE_ID": "zid-shared"})
    seen = {}
    recorded = []

    def fake_resolve_zone_id(client, zone_id, zone_name):
        seen["zone_id"] = zone_id
        seen["zone_name"] = zone_name
        return "resolved-truealpha"

    def fake_ensure_record(client, zone_id, name, ip, proxied, ttl):
        recorded.append(name)
        return True

    monkeypatch.setattr(m, "_resolve_zone_id", fake_resolve_zone_id)
    monkeypatch.setattr(m, "_ensure_record", fake_ensure_record)
    monkeypatch.setattr(m, "_cloudflare_client", lambda token: _FakeClientCtx())

    ok = m._ensure_dns_records(["*", "@"], True, 1, domain="truealpha.club")

    assert ok is True
    # a --domain override zone is ALWAYS resolved by name, never the pinned zid-shared —
    # reusing it would silently write truealpha.club records into the wrong zone (or
    # vice versa) since Cloudflare zone ids are not portable across zones.
    assert seen == {"zone_id": None, "zone_name": "truealpha.club"}
    # records normalize against the override domain, not INTERNAL_DOMAIN.
    assert recorded == ["*.truealpha.club", "truealpha.club"]


class _FakeClientCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
