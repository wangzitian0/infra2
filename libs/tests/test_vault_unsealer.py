"""Vault unsealer health contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UNSEALER = ROOT / "bootstrap/05.vault/unsealer.py"


def _load_unsealer(monkeypatch):
    monkeypatch.setenv("OP_CONNECT_TOKEN", "connect-token")
    monkeypatch.setenv("OP_VAULT_ID", "vault-id")
    monkeypatch.setenv("OP_ITEM_ID", "item-id")
    spec = importlib.util.spec_from_file_location("vault_unsealer_under_test", UNSEALER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vault_unsealer_under_test"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, responses: dict[str, _Response]):
        self.responses = responses
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url: str, **_kwargs) -> _Response:
        self.calls.append(url)
        return self.responses[url]


def test_unsealer_health_rejects_stale_connect_token(monkeypatch) -> None:
    """Infra-011.2: /health 200 is insufficient when bearer auth is 401."""
    unsealer = _load_unsealer(monkeypatch)
    responses = {
        "http://op-connect-api:8080/health": _Response(
            200,
            {
                "dependencies": [
                    {"service": "sqlite", "status": "ACTIVE"},
                    {"service": "sync", "status": "ACTIVE"},
                    {"service": "1Password", "status": "ACTIVE"},
                ]
            },
        ),
        "http://op-connect-api:8080/v1/vaults/vault-id/items/item-id": _Response(
            401
        ),
        "http://vault:8200/v1/sys/health": _Response(200, {"sealed": False}),
    }
    monkeypatch.setattr(
        unsealer.httpx,
        "Client",
        lambda **_kwargs: _Client(responses),
    )

    assert unsealer.health_check() == 1


def test_unsealer_health_requires_active_connect_sync(monkeypatch) -> None:
    """Infra-011.2: TOKEN_NEEDED keeps the unsealer unhealthy."""
    unsealer = _load_unsealer(monkeypatch)
    responses = {
        "http://op-connect-api:8080/health": _Response(
            200,
            {
                "dependencies": [
                    {"service": "sqlite", "status": "ACTIVE"},
                    {"service": "sync", "status": "TOKEN_NEEDED"},
                    {"service": "1Password", "status": "UNINITIALIZED"},
                ]
            },
        ),
        "http://op-connect-api:8080/v1/vaults/vault-id/items/item-id": _Response(
            200
        ),
        "http://vault:8200/v1/sys/health": _Response(200, {"sealed": False}),
    }
    monkeypatch.setattr(
        unsealer.httpx,
        "Client",
        lambda **_kwargs: _Client(responses),
    )

    assert unsealer.health_check() == 1


def test_unsealer_health_initializes_connect_before_health_probe(monkeypatch) -> None:
    """Infra-011.7: bearer-auth lookup must run before Connect /health."""
    unsealer = _load_unsealer(monkeypatch)
    responses = {
        "http://op-connect-api:8080/health": _Response(
            200,
            {
                "dependencies": [
                    {"service": "sqlite", "status": "ACTIVE"},
                    {"service": "sync", "status": "ACTIVE"},
                    {"service": "1Password", "status": "ACTIVE"},
                ]
            },
        ),
        "http://op-connect-api:8080/v1/vaults/vault-id/items/item-id": _Response(
            200
        ),
        "http://vault:8200/v1/sys/health": _Response(200, {"sealed": False}),
    }
    client = _Client(responses)
    monkeypatch.setattr(
        unsealer.httpx,
        "Client",
        lambda **_kwargs: client,
    )

    assert unsealer.health_check() == 0
    assert client.calls[:2] == [
        "http://op-connect-api:8080/v1/vaults/vault-id/items/item-id",
        "http://op-connect-api:8080/health",
    ]
