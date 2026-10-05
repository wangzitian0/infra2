"""The secret supply restarts only the secret consumers that exist (#991 audit).

On the first deploy of a service, the supply writes its values to Vault and then restarts
"the vault-agent and the app containers". Neither container exists yet, `docker restart`
exits 1, and the sync failed before the rest of the deploy could create them. A restart is
for a consumer that holds a stale value, so a consumer that does not exist is skipped.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from libs import secrets_registry
from libs.deploy.deployer import Deployer
from libs.security import supply as supply_module
from libs.service_facets import SecretsFacet
from libs.tests.docker_host import DockerHost

AGENT = "platform-dummy-vault-agent-staging"
APP = "platform-dummy-staging"


class DummyDeployer(Deployer):
    service = "dummy"
    compose_path = "platform/99.dummy/compose.yaml"
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-dummy-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-dummy${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )

    @classmethod
    def env(cls):
        return {"ENV": "staging", "ENV_SUFFIX": "-staging", "VPS_HOST": "vps.example"}


@pytest.fixture()
def supply_changes_a_value(monkeypatch):
    """The supply writes a value, so it asks for the consumers to restart."""
    monkeypatch.setattr(secrets_registry, "lookup", lambda project, service: object())

    def apply(service, env_name, restart=None, resolver=None):
        restart(("SOME_KEY",))
        return SimpleNamespace(
            ok=True, notes=(), missing=(), summary=lambda: "changed=['SOME_KEY']"
        )

    monkeypatch.setattr(supply_module, "apply", apply)


def test_first_deploy_has_no_consumer_to_restart_and_succeeds(
    supply_changes_a_value,
) -> None:
    host = DockerHost(containers={"platform-postgres-staging"})
    assert DummyDeployer.apply_secret_supply(host, env="staging") is True
    assert host.restart_commands == [], "nothing exists yet, so nothing is restarted"


def test_only_the_consumers_that_exist_are_restarted(supply_changes_a_value) -> None:
    host = DockerHost(containers={AGENT, "platform-postgres-staging"})
    assert DummyDeployer.apply_secret_supply(host, env="staging") is True
    assert host.restarted == [AGENT]
    assert len(host.restart_commands) == 1


def test_every_existing_consumer_is_restarted_in_one_command(
    supply_changes_a_value,
) -> None:
    host = DockerHost(containers={AGENT, APP})
    assert DummyDeployer.apply_secret_supply(host, env="staging") is True
    assert sorted(host.restarted) == sorted([AGENT, APP])
    assert len(host.restart_commands) == 1


def test_a_stopped_consumer_is_restarted_too(supply_changes_a_value) -> None:
    """`docker restart` starts a stopped container: a crashed consumer must pick up the
    new value, so existence, not running state, decides."""
    host = DockerHost(containers={AGENT, APP}, running={AGENT})
    assert DummyDeployer.apply_secret_supply(host, env="staging") is True
    assert sorted(host.restarted) == sorted([AGENT, APP])


def test_an_unlistable_host_fails_the_supply_without_restarting(
    supply_changes_a_value,
) -> None:
    """Fail closed: when the host cannot say what exists, consumers may keep a stale
    value while the store changed."""
    host = DockerHost(containers={AGENT, APP}, list_ok=False)
    assert DummyDeployer.apply_secret_supply(host, env="staging") is False
    assert host.restart_commands == []


def test_a_failed_restart_of_an_existing_consumer_fails_the_supply(
    supply_changes_a_value,
) -> None:
    host = DockerHost(containers={AGENT, APP}, restart_ok=False)
    assert DummyDeployer.apply_secret_supply(host, env="staging") is False
    assert len(host.restart_commands) == 1
