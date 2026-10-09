"""signoz.shared.create-api-key stores the key under the environment it was created in.

The task used to read ``DEPLOY_ENV`` from the ``get_env()`` mapping. That mapping has no
such key, so the Vault path was always ``production``: a staging run wrote the staging
SigNoz key into the production store (#1039).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


class _NoOnePasswordRead:
    def get(self, key):
        return None


class _RecordingStore:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value
        return True


def _load_signoz_shared_tasks():
    path = ROOT / "platform/11.signoz/shared_tasks.py"
    spec = importlib.util.spec_from_file_location("signoz_shared_tasks_env_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_create_api_key(monkeypatch, process_environment: str) -> list[tuple]:
    import subprocess

    module = _load_signoz_shared_tasks()
    monkeypatch.setattr(
        "libs.security.store.OpSecrets", lambda *a, **k: _NoOnePasswordRead()
    )
    monkeypatch.setenv("DEPLOY_ENV", process_environment)
    monkeypatch.setenv("INTERNAL_DOMAIN", "example.test")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="admin-credential\n", returncode=0),
    )
    responses = iter(
        [
            json.dumps({"data": {"accessJwt": "jwt"}}),
            json.dumps({"status": "success", "data": {"token": "key", "id": "1"}}),
        ]
    )
    context = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, stdout=next(responses))
    )
    stores: list[tuple] = []

    def fake_get_secrets(project, service, env):
        store = _RecordingStore()
        stores.append((project, service, env, store))
        return store

    monkeypatch.setattr("libs.security.store.get_secrets", fake_get_secrets)

    result = module.create_api_key.body(context)

    assert result and result["api_key"] == "key"
    return stores


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_key_is_stored_under_the_environment_it_was_created_in(
    monkeypatch, environment: str
) -> None:
    stores = _run_create_api_key(monkeypatch, environment)

    assert [(project, service, env) for project, service, env, _ in stores] == [
        ("platform", "signoz", environment)
    ]
    assert stores[0][3].values["api_key"] == "key"
