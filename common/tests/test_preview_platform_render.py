"""T3a (#1647): the Preview render of a platform unit holds throwaway secrets only.

No Dokploy, git or HTTP call is made: the fake client records every call. The tests
read the registered compose file itself, so a Preview that borrows a production Vault
path, an AppRole or a production host fails here.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import yaml

from common.deploy import preview as pl
from common.deploy.env_config import preview_service_config
from common.tests.test_preview_lifecycle import FakeDokploy

REPO_ROOT = Path(__file__).resolve().parents[2]
FULL_SHA = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"
SERVICE = "platform/todo"


@pytest.fixture(autouse=True)
def _stub_resolver(monkeypatch):
    monkeypatch.setattr(pl, "resolve_to_sha", lambda code, repo=None: FULL_SHA)
    # Teardown always polls the inventory twice; no test waits for the backoff.
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)


class NoVaultDokploy(FakeDokploy):
    """A FakeDokploy that fails when the render reads another compose's env (the AppRole)."""

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        assert env_name == "preview", f"read of {name!r} in {env_name!r}"
        return super().find_compose_by_name(name, project_name, env_name)

    def get_compose_env(self, compose_id):
        raise AssertionError("a platform Preview must not read any compose env")


def _render(slot: int) -> tuple[NoVaultDokploy, dict[str, str]]:
    client = NoVaultDokploy(existing=None)
    pl.up(
        "pr",
        slot,
        code="main",
        domain="zitian.party",
        client=client,
        service=SERVICE,
        repo="https://github.com/wangzitian0/infra2",
        http_get=lambda url, timeout: (200, "{}"),
        _sleep=lambda _s: None,
    )
    ((_, env_vars),) = client.env_updates
    return client, env_vars


def _compose_text() -> str:
    return (REPO_ROOT / preview_service_config(SERVICE).compose_path).read_text()


def test_todo_preview_render_holds_no_vault_path_no_approle_no_production_host():
    client, env_vars = _render(5)
    config = preview_service_config(SERVICE)

    # The env the slot receives: the throwaway names, and no Vault or AppRole key.
    assert set(config.throwaway_secrets) <= set(env_vars)
    assert not [k for k in env_vars if re.search(r"VAULT|ROLE_ID|SECRET_ID", k)]
    assert "VAULT_SECRETS_TEMPLATE" not in env_vars
    assert env_vars["ENV_SUFFIX"] == "-pr-5"

    # The compose file: no vault-agent, no Vault path, no AppRole.
    parsed = yaml.safe_load(_compose_text())  # comments are dropped by the parser
    text = yaml.safe_dump(parsed)
    assert not re.search(r"vault|approle|secret/data|ROLE_ID", text, re.IGNORECASE)
    services = parsed["services"]
    assert {"todo", "redis", "postgres", "s3"} <= set(services)

    # Every data host of the slot carries the slot suffix: none is a production or
    # staging container.
    todo_env = services["todo"]["environment"]
    for key in ("POSTGRES_HOST", "REDIS_HOST", "CANARY_S3_ENDPOINT_URL"):
        assert "${ENV_SUFFIX}" in todo_env[key], (key, todo_env[key])
    assert client.created and client.created[0]["name"].endswith("-pr-5")


def test_two_renders_give_different_throwaway_values():
    config = preview_service_config(SERVICE)
    _, first = _render(5)
    _, second = _render(6)
    for name in config.throwaway_secrets:
        assert first[name] != second[name], name
        assert len(first[name]) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", first[name])
    # No value of the first render appears in the second.
    assert not set(first[n] for n in config.throwaway_secrets) & set(
        second[n] for n in config.throwaway_secrets
    )
    # Each secret in one render is distinct from the others of that render.
    assert len({first[n] for n in config.throwaway_secrets}) == len(
        config.throwaway_secrets
    )


def test_app_preview_still_reads_the_source_env_creds():
    # The other side of the branch: an App unit has no throwaway names.
    assert preview_service_config("finance_report/app").throwaway_secrets == ()
    client = FakeDokploy(existing=None)
    pl.up(
        "pr",
        5,
        code="main",
        domain="zitian.party",
        client=client,
        http_get=lambda url, timeout: (
            200,
            '{"git_sha":"1af32e6","version":"1af32e6"}',
        ),
        _sleep=lambda _s: None,
    )
    assert client.env_updates[0][1]["VAULT_ROLE_ID"] == "rid-test"


class _Inventory(FakeDokploy):
    def __init__(self, reads, **kw):
        super().__init__(**kw)
        self._reads = list(reads)
        self.reads = 0

    def get_containers(self):
        self.reads += 1
        row = self._reads.pop(0) if len(self._reads) > 1 else self._reads[0]
        if isinstance(row, Exception):
            raise row
        return row


def test_teardown_always_reads_the_inventory_twice_even_when_the_compose_is_gone():
    assert "wait" not in inspect.signature(pl.down).parameters
    client = _Inventory([[]], existing=None)
    result = pl.down("pr", 5, domain="zitian.party", client=client, service=SERVICE)
    assert client.deleted == []
    assert client.reads == 2
    assert result.containers_cleared is True


def test_teardown_reports_survivors_of_the_slot():
    survivor = [{"name": "/platform-redis-pr-5"}]
    client = _Inventory([survivor], existing={"composeId": "cmp-1"})
    result = pl.down(
        "pr",
        5,
        domain="zitian.party",
        client=client,
        service=SERVICE,
        attempts=3,
        _sleep=lambda _s: None,
    )
    assert client.deleted == [("cmp-1", True)]
    assert result.containers_cleared is False


def _services(path: str) -> dict:
    return yaml.safe_load((REPO_ROOT / path).read_text())["services"]


def test_preview_todo_service_repeats_the_staging_compose_fields():
    # The preview compose is a copy. Every repeated field must stay equal to compose.yaml.
    prod = _services("platform/30.todo/compose.yaml")["todo"]
    slot = _services("platform/30.todo/preview.compose.yaml")["todo"]
    for field in (
        "build",
        "container_name",
        "mem_limit",
        "cpu_shares",
        "logging",
    ):
        assert slot[field] == prod[field], field
    assert slot["healthcheck"]["test"] == prod["healthcheck"]["test"]
    shared = set(slot["environment"]) & set(prod["environment"])
    assert "image" not in slot  # a built service names no image: the runner names it
    assert len(shared) == 18
    assert {
        k for k in shared if slot["environment"][k] != prod["environment"][k]
    } == set()
    # Secrets are the only environment names the slot adds.
    config = preview_service_config(SERVICE)
    assert set(slot["environment"]) - set(prod["environment"]) == set(
        config.throwaway_secrets
    )
    # Every slot label equals a production label, except the slot's own Docker network.
    own = {"traefik.docker.network=dokploy-network"}
    assert set(slot["labels"]) - own <= set(prod["labels"])
    public = [
        x for x in prod["labels"] if "todo-public" in x or "services.platform-todo" in x
    ]
    assert public and set(public) <= set(slot["labels"])


def test_slot_backing_services_use_the_platform_unit_images():
    slot = _services("platform/30.todo/preview.compose.yaml")
    assert (
        slot["redis"]["image"]
        == _services("platform/02.redis/compose.yaml")["redis"]["image"]
    )
    assert (
        slot["postgres"]["image"]
        == _services("platform/01.postgres/compose.yaml")["postgres"]["image"]
    )
    s3 = _services("platform/03.s3/compose.yaml")["s3"]
    assert slot["s3"]["build"] == s3["build"] and "image" not in slot["s3"]
    assert slot["s3-init"]["build"] == s3["build"]


def test_slot_postgres_keeps_no_data_across_up():
    postgres = _services("platform/30.todo/preview.compose.yaml")["postgres"]
    assert any(
        t.startswith("/var/lib/postgresql/data:size=") for t in postgres["tmpfs"]
    )
    assert not postgres.get("volumes")
