"""A deploy is not a success until the stack is in service (#629, #691, #698)."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import common.deploy.deployer as deployer_module
from common.deploy.in_service import (
    DOCKER_PS_FORMAT,
    container_names,
    expected_running_containers,
    expected_running_services,
    in_service_verdict,
    observe_containers,
    parse_docker_ps,
)

ROOT = Path(__file__).resolve().parents[2]

COMPOSE = """
services:
  vault-agent:
    image: hashicorp/vault:1.15
    healthcheck: {test: ["CMD", "true"]}
  backend:
    image: app
    healthcheck: {test: ["CMD", "true"]}
  frontend:
    image: web
    healthcheck: {test: ["CMD", "true"]}
  token-init:
    image: app
    restart: "no"
"""


def _ps(*rows: tuple[str, ...]) -> str:
    """docker ps lines; a row of any other width simulates malformed output."""
    return "\n".join("\t".join(row) for row in rows) + "\n"


def test_the_compose_file_says_which_services_must_stay_up():
    assert expected_running_services(COMPOSE) == ("backend", "frontend", "vault-agent")
    assert expected_running_services("services: {}") == ()


def test_every_registry_compose_expects_its_long_running_services_and_not_its_one_shots():
    authentik = expected_running_services(
        (ROOT / "platform/10.authentik/compose.yaml").read_text()
    )
    assert "server" in authentik and "token-init" not in authentik
    prefect = expected_running_services(
        (ROOT / "platform/23.prefect/compose.yaml").read_text()
    )
    assert "prefect-services" in prefect and "prefect-worker" not in prefect
    signoz = expected_running_services(
        (ROOT / "platform/11.signoz/compose.yaml").read_text()
    )
    assert "schema-migrator" not in signoz


def test_docker_ps_is_parsed_newest_first_per_service():
    observed = parse_docker_ps(
        _ps(
            ("backend", "running", "Up 10 seconds (healthy)"),
            ("backend", "exited", "Exited (137) 12 seconds ago"),  # previous generation
            ("", "running", "Up 4 days"),  # a container without the label
            ("garbage line",),
        )
    )
    assert list(observed) == ["backend"]
    assert observed["backend"].state == "running"
    assert observed["backend"].health == "healthy"
    assert "\t" in DOCKER_PS_FORMAT


def test_a_healthy_stack_is_in_service():
    verdict = in_service_verdict(
        ("backend", "frontend"),
        parse_docker_ps(
            _ps(
                ("backend", "running", "Up 20 seconds (healthy)"),
                ("frontend", "running", "Up 15 seconds (healthy)"),
            )
        ),
    )
    assert verdict.ok and "2 service(s)" in verdict.message


def test_a_frontend_left_in_created_is_a_verdict_not_a_delay():
    """#698: the backend's migration failed, `depends_on: service_healthy` left the
    frontend `Created`, and the site 404'd although the deploy 'finished'."""
    verdict = in_service_verdict(
        ("backend", "frontend"),
        parse_docker_ps(
            _ps(
                ("backend", "running", "Up 3 minutes (unhealthy)"),
                ("frontend", "created", "Created"),
            )
        ),
    )
    assert not verdict.ok and not verdict.settling
    assert "backend is unhealthy" in verdict.message
    assert "frontend is created (Created)" in verdict.message


def test_no_container_at_all_is_a_verdict():
    """#691: the record said done, the hash was recorded, nothing was created."""
    verdict = in_service_verdict(("postgres", "vault-agent"), {})
    assert not verdict.ok and not verdict.settling
    assert verdict.message == "no container for postgres, vault-agent"


def test_only_a_health_check_still_starting_is_worth_waiting_for():
    starting = in_service_verdict(
        ("backend",),
        parse_docker_ps(_ps(("backend", "running", "Up 2 seconds (health: starting)"))),
    )
    assert not starting.ok and starting.settling
    assert "health still starting for backend" == starting.message
    restarting = in_service_verdict(
        ("backend",),
        parse_docker_ps(_ps(("backend", "restarting", "Restarting (1) 3 seconds ago"))),
    )
    assert not restarting.ok and not restarting.settling


def test_created_container_settles_while_peers_are_healthy():
    """#942: during initial startup, a newly created container transitions from
    Created to Running. If all peer containers are healthy, it should settle."""
    verdict = in_service_verdict(
        ("s3", "vault-agent"),
        parse_docker_ps(
            _ps(
                ("s3", "created", "Created"),
                ("vault-agent", "running", "Up 10 seconds (healthy)"),
            )
        ),
    )
    assert not verdict.ok and verdict.settling
    assert "s3 is created (Created)" in verdict.message


class _Run:
    def __init__(self, ok: bool, stdout: str = "", stderr: str = ""):
        self.ok, self.stdout, self.stderr = ok, stdout, stderr


class _Context:
    """Scripted host answers, in call order; records every ssh command."""

    def __init__(self, answers: list[_Run]):
        self.answers = list(answers)
        self.commands: list[str] = []

    def run(self, cmd, hide=True, warn=True):
        self.commands.append(cmd)
        return self.answers.pop(0)


@pytest.fixture
def deployer(monkeypatch):
    class D(deployer_module.Deployer):
        service = "app"
        compose_path = "x/compose.yaml"
        IN_SERVICE_INTERVAL_SECONDS = 0

        @classmethod
        def env(cls):
            return {
                "ENV": "production",
                "VPS_HOST": "vps",
                "INTERNAL_DOMAIN": "example.test",
            }

        @classmethod
        def get_compose_content(cls, c):
            return COMPOSE

        @classmethod
        def _checkout_ref(cls):
            return "v1.2.3"

        @classmethod
        def _checkout_sha(cls, ref):
            return "a" * 40

    monkeypatch.setattr(
        "common.deploy.dokploy_client.get_dokploy",
        lambda host=None: SimpleNamespace(
            get_compose=lambda compose_id: {"appName": "platform-app-abc123"}
        ),
    )
    monkeypatch.setattr(deployer_module.time, "sleep", lambda _s: None)
    return D


def test_verify_in_service_waits_for_starting_health_then_passes(deployer):
    healthy = _ps(
        ("backend", "running", "Up 9 seconds (healthy)"),
        ("frontend", "running", "Up 9 seconds (healthy)"),
        ("vault-agent", "running", "Up 9 seconds (healthy)"),
        ("token-init", "exited", "Exited (0) 8 seconds ago"),
    )
    starting = healthy.replace(
        "frontend\trunning\tUp 9 seconds (healthy)",
        "frontend\trunning\tUp 1 second (health: starting)",
    )
    c = _Context(
        [_Run(True, "a" * 40 + "\n"), _Run(True, starting), _Run(True, healthy)]
    )
    assert deployer.verify_in_service(c, "cid") is None
    assert (
        "git -C /etc/dokploy/compose/platform-app-abc123/code rev-parse HEAD"
        in c.commands[0]
    )
    assert "label=com.docker.compose.project=platform-app-abc123" in c.commands[1]
    assert len(c.commands) == 3


def test_verify_in_service_fails_on_a_stale_clone_before_looking_at_containers(
    deployer,
):
    """#629: the clone failed after the record was created; prod kept the old code
    with a green receipt. The checkout's HEAD has to be the ref this deploy pinned."""
    c = _Context([_Run(True, "b" * 40 + "\n")])
    message = deployer.verify_in_service(c, "cid")
    assert message and "stale clone" in message and "v1.2.3" in message
    assert len(c.commands) == 1
    unreadable = _Context([_Run(False, "", "fatal: not a git repository")])
    assert "no readable HEAD" in deployer.verify_in_service(unreadable, "cid")


def test_verify_in_service_fails_when_the_pinned_ref_cannot_be_resolved(
    deployer, monkeypatch
):
    """Fail-closed (review): without a resolvable pinned ref the identity proof is
    impossible, and an unprovable deploy is not a success."""
    monkeypatch.setattr(deployer, "_checkout_sha", classmethod(lambda cls, ref: None))
    c = _Context([])
    message = deployer.verify_in_service(c, "cid")
    assert message and "cannot resolve the ref it pinned" in message
    assert c.commands == []


def test_verify_in_service_reports_the_created_frontend(deployer):
    c = _Context(
        [
            _Run(True, "a" * 40 + "\n"),
            _Run(
                True,
                _ps(
                    ("backend", "running", "Up 3 minutes (unhealthy)"),
                    ("frontend", "created", "Created"),
                    ("vault-agent", "running", "Up 3 minutes (healthy)"),
                ),
            ),
        ]
    )
    message = deployer.verify_in_service(c, "cid")
    assert (
        message
        == "backend is unhealthy (Up 3 minutes (unhealthy)); frontend is created (Created)"
    )


def test_verify_in_service_gives_up_on_a_health_check_that_never_finishes(
    deployer, monkeypatch
):
    clock = iter([0.0, 0.0, 500.0, 500.0])
    monkeypatch.setattr(deployer_module.time, "monotonic", lambda: next(clock))
    starting = _ps(
        ("backend", "running", "Up 1 second (health: starting)"),
        ("frontend", "running", "Up 1 second (healthy)"),
        ("vault-agent", "running", "Up 1 second (healthy)"),
    )
    c = _Context(
        [_Run(True, "a" * 40 + "\n"), _Run(True, starting), _Run(True, starting)]
    )
    assert deployer.verify_in_service(c, "cid") == "health still starting for backend"


def test_verify_in_service_is_skipped_without_a_host(deployer, monkeypatch):
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {"ENV": "production"}))
    c = _Context([])
    assert deployer.verify_in_service(c, "cid") is None and c.commands == []


def test_sync_reports_failed_when_the_stack_is_not_in_service(monkeypatch):
    """The wiring: a deploy whose record, hash and identity all check out still fails
    when the containers are not in service."""
    from unittest.mock import MagicMock

    d = deployer_module

    class NotInService(d.Deployer):
        service = "postgres"
        compose_path = "platform/01.postgres/compose.yaml"

        @classmethod
        def env(cls):
            return {"ENV": "production", "VPS_HOST": "vps", "INTERNAL_DOMAIN": "x.test"}

        @classmethod
        def verify_in_service(cls, c, compose_id):
            return "no container for postgres"

    monkeypatch.setattr(
        NotInService, "ensure_runtime_secrets", classmethod(lambda cls, c: True)
    )
    monkeypatch.setattr(
        NotInService, "apply_secret_supply", classmethod(lambda cls, c, env=None: True)
    )
    monkeypatch.setattr(
        NotInService, "compose_env_base", classmethod(lambda cls, e: {})
    )
    monkeypatch.setattr(
        NotInService, "source_config_env_base", classmethod(lambda cls, e: {})
    )
    monkeypatch.setattr(
        NotInService,
        "config_env_with_vault_addr",
        classmethod(lambda cls, env, e: dict(env)),
    )
    monkeypatch.setattr(
        NotInService, "compute_local_config_hash", classmethod(lambda cls, c, env: "h1")
    )
    monkeypatch.setattr(
        NotInService, "get_remote_config_hash", classmethod(lambda cls: "h0")
    )
    monkeypatch.setattr(
        NotInService, "composing", classmethod(lambda cls, c, env: "cid")
    )
    monkeypatch.setattr(
        NotInService, "_await_effective_config_hash", classmethod(lambda cls, h: "h1")
    )
    monkeypatch.setattr(
        NotInService, "verify_runtime_applied", classmethod(lambda cls, c, env: None)
    )

    class _Matches:
        """Whatever sync computed for the identity plane; this test is about the tail."""

        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

        __hash__ = object.__hash__

    class _RemoteIdentity(dict):
        def __getitem__(self, key):
            if key == "runtime_hash":
                return "h0"
            if key == "deploy_ref":
                return "a" * 40
            return _Matches()

        def get(self, key, default=None):
            return self[key]

    monkeypatch.setattr(
        NotInService,
        "get_remote_config_identity",
        classmethod(lambda cls: _RemoteIdentity()),
    )
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setenv("IAC_DEPLOY_REF", "a" * 40)

    result = NotInService.sync(MagicMock())
    assert result["action"] == "failed"
    assert "no container for postgres" in result["details"]


RELEASE_SHA = "a" * 40  # this runner's checkout: the release being synced (v1.2.4)
LAST_DEPLOY_SHA = "b" * 40  # the release the stack was last deployed from

REDIS = "platform/02.redis/compose.yaml"
# platform/02.redis is a raw x-infra2 stack (#1822 step B). Its compiled text declares
# the redis service and the vault-agent sidecar, both with a healthcheck.
REDIS_UP = _ps(
    ("redis", "running", "Up 3 days (healthy)"),
    ("vault-agent", "running", "Up 3 days (healthy)"),
)
REDIS_EXITED = _ps(
    ("redis", "exited", "Exited (137) 2 hours ago"),
    ("vault-agent", "running", "Up 3 days (healthy)"),
)

# The sample of a github (git-checkout) stack: platform/21.portal is plain and has no
# Vault sidecar, so #1822 step B does not move it. Dokploy clones it and the
# #629/#718/#1247/#1439 proofs read the HEAD. platform/23.prefect was the sample until it
# moved to x-infra2 (#1822 step B).
PLAIN = "platform/21.portal/compose.yaml"
PLAIN_COMPOSE = """
services:
  portal:
    image: nginx:1
    healthcheck: {test: ["CMD", "true"]}
"""
PLAIN_UP = _ps(("portal", "running", "Up 3 days (healthy)"))


class _Host:
    """The VPS over ssh: Dokploy's checkout HEAD, the stack's containers, and every
    running container by name (what the dependent restart looks at, #726)."""

    def __init__(
        self,
        checkout: str | None,
        containers: str,
        *,
        listable: bool = True,
        running: tuple[str, ...] = (),
        restart_ok: bool = True,
        raw: tuple[str, Callable[[], str | None]] | None = None,
    ):
        """`checkout` is None for a raw stack: it has no clone, so `git rev-parse` is
        an unexpected command. `raw` is (the compose file Dokploy wrote, a callable that
        answers the CONFIGURATION_SHA256 that the running containers carry)."""
        self.checkout, self.containers, self.listable = checkout, containers, listable
        self.running, self.restart_ok, self.raw = running, restart_ok, raw
        self.commands: list[str] = []

    def run(self, cmd, hide=True, warn=True):
        self.commands.append(cmd)
        if "rev-parse HEAD" in cmd and self.checkout is not None:
            return _Run(True, self.checkout + "\n")
        if self.raw and cmd.endswith("/code/docker-compose.yml'"):
            return _Run(True, self.raw[0])
        if self.raw and "docker inspect" in cmd:
            carried = {"CONFIGURATION_SHA256": self.raw[1]() or ""}
            return _Run(
                True,
                _inspected(
                    ("platform-redis", carried), ("platform-redis-vault-agent", carried)
                ),
            )
        if "{{.Names}}" in cmd:
            return _Run(True, "".join(f"{name}\n" for name in self.running))
        if "docker ps" in cmd:
            if not self.listable:
                return _Run(False, "", "ssh: connect to host vps port 22: timed out")
            return _Run(True, self.containers)
        if "docker restart" in cmd:
            if self.restart_ok:
                return _Run(True, "")
            return _Run(False, "", "Error response from daemon: container is paused")
        raise AssertionError(f"unexpected host command: {cmd}")

    @property
    def asked_for_the_checkout(self) -> bool:
        return any("rev-parse" in cmd for cmd in self.commands)

    @property
    def restarted(self) -> list[list[str]]:
        """The containers named by each `docker restart` sent over ssh."""
        restarts = []
        for cmd in self.commands:
            ssh, _destination, remote = shlex.split(cmd)
            argv = shlex.split(remote)
            if argv[:2] == ["docker", "restart"]:
                restarts.append(argv[2:])
        return restarts


class _Dokploy:
    """platform/<name> in `env`; any other lookup finds nothing."""

    def __init__(self, env: str = "production", name: str = "redis"):
        self.env, self.name = env, name

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        if (name, project_name, env_name) == (self.name, "platform", self.env):
            return {"composeId": "cid", "appName": f"platform-{self.name}-x1"}
        return None

    def get_compose(self, compose_id):
        assert compose_id == "cid"
        return {"composeId": "cid", "appName": f"platform-{self.name}-x1"}


def _redis_sync(
    monkeypatch, *, remote_hash: str, env: str = "production", suffix: str = ""
):
    """A platform/redis sync (a raw stack) in `env` at RELEASE_SHA against a stack last
    deployed from LAST_DEPLOY_SHA; `remote_hash` "h1" means its config did not change.
    Returns the deployer and the list `composing` appends to when a (re)deploy is
    triggered."""
    return _stack_sync(
        monkeypatch,
        name="redis",
        compose_path=REDIS,
        content=(ROOT / REDIS).read_text(encoding="utf-8"),
        remote_hash=remote_hash,
        env=env,
        suffix=suffix,
    )


def _plain_sync(monkeypatch, *, remote_hash: str):
    """The same sync for a github stack (PLAIN), which Dokploy clones at a ref."""
    return _stack_sync(
        monkeypatch,
        name="portal",
        compose_path=PLAIN,
        content=PLAIN_COMPOSE,
        remote_hash=remote_hash,
    )


def _raw_redis_host(redis, **kwargs) -> _Host:
    """The host of a raw redis: no clone, Dokploy's compiled file, bound containers."""
    from common.deploy.compose_compile import load_stack

    assert redis._raw_identity_labels(), "platform/02.redis must be a raw stack"
    written = load_stack(REDIS).compiled
    return _Host(None, raw=(written, redis.get_remote_config_hash), **kwargs)


def _stack_sync(
    monkeypatch,
    *,
    name: str,
    compose_path: str,
    content: str,
    remote_hash: str,
    env: str = "production",
    suffix: str = "",
):
    from common.core.service_identity import ServiceIdentity

    d = deployer_module
    path = compose_path
    identity = ServiceIdentity.build(
        f"platform/{name}", env, component=name, service_name=name
    ).deploy_env()
    remote = {
        "runtime_hash": remote_hash,
        "source_hash": f"{d.SOURCE_CONFIG_HASH_VERSION}:h1",
        "deploy_ref": LAST_DEPLOY_SHA,
        "identity_schema": identity["INFRA_IDENTITY_SCHEMA"],
        "managed_by": identity["INFRA_MANAGED_BY"],
        "service_id": identity["INFRA_SERVICE_ID"],
        "environment": identity["INFRA_ENVIRONMENT"],
    }
    deployed: list[dict] = []

    class Stack(d.Deployer):
        service = name
        compose_path = path
        IN_SERVICE_INTERVAL_SECONDS = 0

        @classmethod
        def env(cls):
            return {
                "ENV": env,
                "ENV_SUFFIX": suffix,
                "VPS_HOST": "vps",
                "INTERNAL_DOMAIN": "x.test",
            }

        @classmethod
        def ensure_runtime_secrets(cls, c):
            return True

        @classmethod
        def apply_secret_supply(cls, c, env=None):
            return True

        @classmethod
        def compose_env_base(cls, e):
            return {}

        @classmethod
        def source_config_env_base(cls, e):
            return {}

        @classmethod
        def config_env_with_vault_addr(cls, env, e):
            return dict(env)

        @classmethod
        def compute_local_config_hash(cls, c, env):
            return "h1"

        @classmethod
        def get_compose_content(cls, c):
            return content

        @classmethod
        def get_remote_config_identity(cls):
            return dict(remote)

        @classmethod
        def _prepare_dirs(cls, c):
            return True

        @classmethod
        def composing(cls, c, env):
            deployed.append(env)
            remote.update(
                runtime_hash=env["CONFIGURATION_SHA256"],
                source_hash=env["IAC_SOURCE_CONFIG_HASH"],
                deploy_ref=env["IAC_DEPLOY_REF"],
            )
            return "cid"

        @classmethod
        def _await_effective_config_hash(cls, expected):
            return remote["runtime_hash"]

        @classmethod
        def _checkout_ref(cls):
            return "v1.2.4"

        @classmethod
        def _checkout_sha(cls, ref):
            return RELEASE_SHA

    monkeypatch.setattr(
        "common.deploy.dokploy_client.get_dokploy",
        lambda host=None: _Dokploy(env, name),
    )
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setattr(d.time, "sleep", lambda _s: None)
    monkeypatch.setenv("IAC_DEPLOY_REF", RELEASE_SHA)
    monkeypatch.delenv("DEPLOY_ACTION", raising=False)
    return Stack, deployed


def test_an_unchanged_stack_at_an_older_checkout_is_skipped_not_restarted(
    monkeypatch,
):
    """#718 regression: the skip path asked the post-deploy identity proof, which
    wants Dokploy's checkout at THIS release; an unchanged stack is at its last
    deploy's, so every release would have restarted every unchanged service (a redis
    restart breaks OpenPanel and Authentik workers, #726). A github stack keeps the
    checkout proof, so PLAIN is the sample (platform/02.redis is raw since #1822)."""
    stack, deployed = _plain_sync(monkeypatch, remote_hash="h1")
    host = _Host(checkout=LAST_DEPLOY_SHA, containers=PLAIN_UP)

    result = stack.sync(host)

    assert result["action"] == "skipped", result
    assert deployed == []
    assert not host.asked_for_the_checkout
    assert any(
        "label=com.docker.compose.project=platform-portal-x1" in cmd
        for cmd in host.commands
    )


def test_an_unchanged_stack_with_an_exited_container_is_redeployed(monkeypatch):
    """#691/#698, the intent of #718: matching config is no reason to leave a dead
    container dead. (This also pins the compose lookup: #718 passed the project as
    the compose name, which found nothing and skipped the check entirely.)"""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h1")
    host = _Host(checkout=LAST_DEPLOY_SHA, containers=REDIS_EXITED)

    redis.sync(host)

    assert len(deployed) == 1
    assert deployed[0]["IAC_DEPLOY_REF"] == RELEASE_SHA


def test_an_unchanged_stack_on_an_unreachable_host_fails_not_skips(monkeypatch):
    """#1897: unobservable is neither dead nor alive. A host that cannot list the
    containers fails the sync (as verify_in_service does after a deploy); it is not a
    skip, and it is not a redeploy onto a host that did not answer."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h1")
    monkeypatch.setattr(redis, "IN_SERVICE_DEADLINE_SECONDS", 0)
    errors: list[str] = []
    monkeypatch.setattr(deployer_module, "error", errors.append)
    host = _Host(checkout=LAST_DEPLOY_SHA, containers="", listable=False)

    result = redis.sync(host)

    assert result["action"] == "failed", result
    assert deployed == []
    assert "not proven in service" in result["details"]
    assert "timed out" in result["details"]
    assert any(
        "could not check the unchanged stack is in service" in e
        and "timed out" in e
        and "failing this service's sync" in e
        for e in errors
    )


def test_an_unchanged_stack_whose_compose_lookup_raises_fails_not_skips(monkeypatch):
    """#1897: a Dokploy API error on the skip-path compose lookup is no proof either."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h1")

    def _lookup_fails(cls, e):
        raise RuntimeError("dokploy 502 bad gateway")

    monkeypatch.setattr(redis, "_find_remote_compose", classmethod(_lookup_fails))
    host = _Host(checkout=LAST_DEPLOY_SHA, containers=REDIS_UP)

    result = redis.sync(host)

    assert result["action"] == "failed", result
    assert deployed == []
    assert "dokploy 502 bad gateway" in result["details"]
    assert host.commands == []


def test_a_changed_stack_still_fails_on_a_stale_checkout_after_deploy(monkeypatch):
    """#629 is untouched: after THIS release deployed the stack, a Dokploy checkout
    left at the previous release is a failed deploy, healthy containers or not."""
    stack, deployed = _plain_sync(monkeypatch, remote_hash="h0")
    host = _Host(checkout=LAST_DEPLOY_SHA, containers=PLAIN_UP)

    result = stack.sync(host)

    assert len(deployed) == 1
    assert result["action"] == "failed"
    assert "stale clone" in result["details"] and "v1.2.4" in result["details"]


def test_a_changed_stack_at_the_new_checkout_is_updated(monkeypatch):
    stack, deployed = _plain_sync(monkeypatch, remote_hash="h0")
    host = _Host(checkout=RELEASE_SHA, containers=PLAIN_UP)

    result = stack.sync(host)

    # no dependent is running on this host, so none was restarted
    assert result == {"action": "updated", "details": "composeId: cid"}
    assert host.asked_for_the_checkout
    assert host.restarted == []


def _names(compose: str, suffix: str, *services: str) -> list[str]:
    """Container names from the compose file itself, not literals."""
    declared = container_names((ROOT / compose).read_text(), suffix)
    return (
        [declared[service] for service in services]
        if services
        else list(declared.values())
    )


OPENPANEL = "platform/24.openpanel/compose.yaml"
AUTHENTIK = "platform/10.authentik/compose.yaml"


def _everything_running(suffix: str) -> tuple[str, ...]:
    return tuple(
        _names(REDIS, suffix) + _names(OPENPANEL, suffix) + _names(AUTHENTIK, suffix)
    )


def test_a_skipped_redis_sync_restarts_no_dependent(monkeypatch):
    """#726: only a sync that recreated Redis flushes its script cache; a skip must
    not bounce OpenPanel and Authentik on every release (#718)."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h1")
    host = _raw_redis_host(redis, containers=REDIS_UP, running=_everything_running(""))

    result = redis.sync(host)

    assert result["action"] == "skipped" and deployed == []
    assert "restarted_dependents" not in result
    assert host.restarted == []
    assert not any("{{.Names}}" in cmd for cmd in host.commands)
    # the raw skip path binds the containers to the release (#1893), with no clone
    assert any("docker inspect" in cmd for cmd in host.commands)
    assert not host.asked_for_the_checkout


def test_an_applied_redis_sync_restarts_exactly_its_declared_dependents(monkeypatch):
    """#726: after Redis is redeployed and in service, OpenPanel api + worker and the
    Authentik worker are restarted in one call — and nothing else (not the OpenPanel
    dashboard, not the Authentik server, not Redis itself)."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h0")
    printed: list[str] = []
    monkeypatch.setattr(deployer_module, "success", printed.append)
    host = _raw_redis_host(redis, containers=REDIS_UP, running=_everything_running(""))

    result = redis.sync(host)

    expected = _names(AUTHENTIK, "", "worker") + _names(
        OPENPANEL, "", "op-api", "op-worker"
    )
    assert result["action"] == "updated", result
    assert len(deployed) == 1
    assert host.restarted == [expected]
    assert result["restarted_dependents"] == expected
    # the runner prints the deployer's ✅ lines (sync_runner.verdict_lines)
    assert any(
        "restarted dependents after redeploy (production)" in line
        and all(name in line for name in expected)
        for line in printed
    )
    # the restart comes after the in-service proof, never before it
    in_service = next(
        i for i, cmd in enumerate(host.commands) if "com.docker.compose.project" in cmd
    )
    restart = next(i for i, cmd in enumerate(host.commands) if "docker restart" in cmd)
    assert in_service < restart
    # a raw redis proves the file Dokploy wrote and the release binding, not a HEAD
    written = next(
        i for i, cmd in enumerate(host.commands) if "code/docker-compose.yml" in cmd
    )
    bound = next(i for i, cmd in enumerate(host.commands) if "docker inspect" in cmd)
    assert written < in_service < bound < restart
    assert not host.asked_for_the_checkout


def test_a_staging_redis_sync_restarts_only_staging_dependents(monkeypatch):
    """OpenPanel is prod_only (no staging instance), so a staging Redis restarts only
    Authentik's staging worker — never a production container."""
    redis, _deployed = _redis_sync(
        monkeypatch, remote_hash="h0", env="staging", suffix="-staging"
    )
    host = _raw_redis_host(
        redis,
        containers=REDIS_UP,
        running=_everything_running("") + _everything_running("-staging"),
    )

    result = redis.sync(host)

    assert result["action"] == "updated", result
    assert host.restarted == [_names(AUTHENTIK, "-staging", "worker")]
    assert host.restarted == [["platform-authentik-worker-staging"]]


def test_a_dependent_that_is_not_running_is_left_alone(monkeypatch):
    redis, _deployed = _redis_sync(monkeypatch, remote_hash="h0")
    warnings: list[str] = []
    monkeypatch.setattr(deployer_module, "warning", warnings.append)
    running = tuple(
        name
        for name in _everything_running("")
        if name not in _names(OPENPANEL, "", "op-worker")
    )
    host = _raw_redis_host(redis, containers=REDIS_UP, running=running)

    result = redis.sync(host)

    assert result["action"] == "updated"
    assert host.restarted == [
        _names(AUTHENTIK, "", "worker") + _names(OPENPANEL, "", "op-api")
    ]
    assert any(
        "not running, not restarted" in w and _names(OPENPANEL, "", "op-worker")[0] in w
        for w in warnings
    )


def test_a_failed_dependent_restart_fails_the_sync_with_the_manual_command(
    monkeypatch,
):
    """A retry would skip the now-unchanged Redis and never restart them, so the sync
    fails and says what to run by hand."""
    redis, _deployed = _redis_sync(monkeypatch, remote_hash="h0")
    host = _raw_redis_host(
        redis,
        containers=REDIS_UP,
        running=_everything_running(""),
        restart_ok=False,
    )

    result = redis.sync(host)

    assert result["action"] == "failed"
    assert "dependents were not restarted" in result["details"]
    assert "container is paused" in result["details"]
    assert (
        "restart by hand: docker restart platform-authentik-worker "
        "platform-openpanel-api platform-openpanel-worker" in result["details"]
    )


def test_restart_dependents_needs_a_host(monkeypatch):
    redis, _deployed = _redis_sync(monkeypatch, remote_hash="h0")
    with pytest.raises(RuntimeError, match="VPS_HOST unset; restart by hand"):
        redis.restart_dependents(_Host(RELEASE_SHA, REDIS_UP), {"ENV": "production"})
    # a deployer outside the registry never scans it and needs no host at all
    monkeypatch.setattr(
        "common.core.registry.service_attrs",
        lambda: (_ for _ in ()).throw(AssertionError("must not scan the registry")),
    )
    assert deployer_module.Deployer.restart_dependents(_Host(RELEASE_SHA, ""), {}) == []


def test_verify_in_service_still_reports_an_unlistable_host_as_a_failure(
    deployer, monkeypatch
):
    """The post-deploy proof keeps its fail-closed message; only the skip path treats
    an unobservable host as no evidence."""
    monkeypatch.setattr(deployer, "IN_SERVICE_DEADLINE_SECONDS", 0)
    c = _Context([_Run(True, "a" * 40 + "\n"), _Run(False, "", "permission denied")])
    assert (
        deployer.verify_in_service(c, "cid")
        == "could not list platform-app-abc123's containers on vps: permission denied"
    )
    with pytest.raises(deployer_module._StackUnobservable, match="permission denied"):
        deployer.verify_still_in_service(
            _Context([_Run(False, "", "permission denied")]), "cid"
        )


def test_container_names_resolve_the_env_suffix_for_the_promote_tier():
    compose = (ROOT / "finance_report/finance_report/10.app/compose.yaml").read_text()
    staging = expected_running_containers(compose, "-staging")
    assert staging["frontend"] == "finance_report-frontend-staging"
    assert staging["backend"] == "finance_report-backend-staging"
    assert (
        expected_running_containers(compose, "")["frontend"]
        == "finance_report-frontend"
    )
    assert (
        expected_running_containers(
            "services:\n  x:\n    healthcheck: {test: [CMD, true]}\n", ""
        )
        == {}
    )


def test_dokploy_container_listing_is_keyed_by_service_and_omits_the_absent():
    observed = observe_containers(
        {"backend": "app-backend", "frontend": "app-frontend"},
        [
            {
                "name": "app-backend",
                "state": "running",
                "status": "Up 2 minutes (unhealthy)",
            },
            {"name": "unrelated", "state": "running", "status": "Up 4 days"},
        ],
    )
    assert list(observed) == ["backend"] and observed["backend"].health == "unhealthy"
    verdict = in_service_verdict(("backend", "frontend"), observed)
    assert not verdict.ok and "no container for frontend" in verdict.message


def test_verify_still_in_service_detects_mismatched_recorded_deploy_ref_checkout(
    monkeypatch,
):
    """#1247: when a prior sync updated IAC_DEPLOY_REF in Dokploy DB but Dokploy's
    on-disk git checkout stayed at an older SHA, verify_still_in_service returns an
    error string so SyncPipeline forces a redeploy instead of skipping. Only a github
    stack has that checkout, so PLAIN is the sample (platform/02.redis is raw)."""
    stack, deployed = _plain_sync(monkeypatch, remote_hash="h1")
    monkeypatch.setattr(
        stack,
        "_find_remote_compose",
        classmethod(
            lambda cls, e: {
                "composeId": "cid",
                "appName": "platform-portal-x1",
                "env": f"IAC_DEPLOY_REF={LAST_DEPLOY_SHA}\n",
            }
        ),
    )
    stale_on_disk_sha = "c" * 40
    host = _Host(checkout=stale_on_disk_sha, containers=PLAIN_UP)

    err = stack.verify_still_in_service(host, "cid")
    assert err is not None
    assert stale_on_disk_sha[:12] in err
    assert LAST_DEPLOY_SHA[:12] in err


def _records(*statuses):  # oldest first, the newest last
    return [
        {"deploymentId": f"d{n}", "status": s, "createdAt": f"2026-11-05T0{n}:00"}
        for n, s in enumerate(statuses)
    ]


@pytest.mark.parametrize(
    ("statuses", "redeploys"),
    [
        (("done", "error"), True),
        (("done", "running"), True),
        (("error", "done"), False),
        ((), False),
    ],
)
def test_an_unchanged_stack_is_skipped_only_when_its_newest_record_is_done(
    monkeypatch, statuses, redeploys
):
    """#1439: a runner killed after it wrote the new hash leaves hash, checkout and the old
    containers matching while Dokploy's deploy runs or failed; the skip must not turn the
    retry green. No record keeps the skip. The redeploy's newer `done` record ends the block."""
    records = _records(*statuses)
    stack, deployed = _plain_sync(monkeypatch, remote_hash="h1")
    monkeypatch.setattr(
        "common.deploy.dokploy_adapter.get_compose_deployments", lambda _c, _id: records
    )
    host = _Host(checkout=LAST_DEPLOY_SHA, containers=PLAIN_UP)

    stack.sync(host)
    assert (len(deployed) == 1) is redeploys
    records.append({"status": "done", "createdAt": "2026-11-05T09:00"})
    assert stack.sync(host)["action"] == "skipped" and len(deployed) == int(redeploys)


def test_composing_restores_existing_env_when_deploy_fails(monkeypatch):
    """#1247: if _deploy_compose_with_record_check raises after upsert_github_compose
    wrote the new env to Dokploy DB, composing() restores existing_env so the DB does
    not falsely claim the failed IAC_DEPLOY_REF / CONFIGURATION_SHA256."""
    d = deployer_module

    class FailingDeployer(d.Deployer):
        service = "authentik"
        # A plain (github) compose: platform/10.authentik is x-infra2 since #1822 step B,
        # and this test writes the github upsert first.
        compose_path = "platform/24.openpanel/compose.yaml"

        @classmethod
        def env(cls):
            return {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"}

        @classmethod
        def _assert_approle_creds_present(cls, effective_env):
            return None

        @classmethod
        def _prune_stale_dokploy_domains(cls, client, compose_id):
            return None

        @classmethod
        def ensure_compose_domains(cls, client, compose_id, e):
            return {}

        @classmethod
        def _deploy_compose_with_record_check(cls, client, compose_id):
            raise RuntimeError("Dokploy deployment record entered error")

    updates: list[dict] = []

    class FakeClient:
        def get_github_provider_id(self):
            return "gh-1"

        def find_compose_by_name(self, name, project_name, env_name=None):
            return {"composeId": "cmp-auth"}

        def get_compose(self, compose_id):
            return {
                "composeId": compose_id,
                "env": "IAC_DEPLOY_REF=old_sha\n",
                "sourceType": "raw",
                "composePath": "./docker-compose.yml",
                "branch": "old_sha",
            }

        def update_compose(self, compose_id, **kwargs):
            updates.append({"compose_id": compose_id, **kwargs})
            return {}

    monkeypatch.setattr(
        "common.deploy.dokploy_client.get_dokploy", lambda host=None: FakeClient()
    )
    monkeypatch.setattr(
        "common.deploy.dokploy_client.ensure_project",
        lambda *args, **kwargs: ("proj-1", "env-1"),
    )

    with pytest.raises(RuntimeError, match="Dokploy deployment record entered error"):
        FailingDeployer.composing(object(), {"IAC_DEPLOY_REF": "new_sha"})

    assert len(updates) == 2
    assert updates[0]["env"] == "IAC_DEPLOY_REF=new_sha"
    assert updates[0]["source_type"] == "github"
    # The restore puts back the source of the record, not only its env (infra2#1822).
    assert updates[1] == {
        "compose_id": "cmp-auth",
        "env": "IAC_DEPLOY_REF=old_sha\n",
        "source_type": "raw",
        "composePath": "./docker-compose.yml",
        "branch": "old_sha",
        "autoDeploy": False,
    }


def test_a_failed_deploy_restores_the_whole_compose_source():
    """infra2#1822: a raw upsert over a github record changes the source type, the
    compose text and the compose path. A failed deploy puts each one back, not only the
    env; else the record keeps the new compose with the old identity."""
    from common.deploy.dokploy_adapter import raw_source_fields, source_snapshot

    github_record = {
        "composeId": "cmp-todo",
        "sourceType": "github",
        "composeFile": "",
        "composePath": "platform/30.todo/compose.yaml",
        "githubId": "gh-1",
        "repository": "infra2",
        "owner": "wangzitian0",
        "branch": "old_sha",
        "env": "IAC_DEPLOY_REF=old_sha\n",
        "name": "todo",
    }
    snapshot = source_snapshot(github_record)
    assert snapshot == {
        "source_type": "github",
        "compose_file": "",
        "composePath": "platform/30.todo/compose.yaml",
        "githubId": "gh-1",
        "repository": "infra2",
        "owner": "wangzitian0",
        "branch": "old_sha",
        "env": "IAC_DEPLOY_REF=old_sha\n",
        "autoDeploy": False,
    }
    # The snapshot puts back each field that the raw upsert writes.
    raw = raw_source_fields("services: {}\n")
    overwritten = {k for k in raw if k != "autoDeploy"}
    assert overwritten <= set(snapshot), overwritten - set(snapshot)


def test_wait_for_rollout_and_failure_snapshot_surface_dokploy_log_tail(
    tmp_path, monkeypatch
):
    """#1366: wait_for_rollout and failure_snapshot.build_snapshot surface deploymentId,
    logPath, errorMessage, and log tail when a Dokploy deployment record enters error."""
    from common.deploy import failure_snapshot
    from common.deploy.dokploy_adapter import read_deployment_log_tail
    from common.deploy.failure_snapshot import build_snapshot
    from common.deploy.steps import app_rollout
    from common.deploy.steps.app_rollout import wait_for_rollout

    monkeypatch.setattr(
        app_rollout,
        "read_deployment_log_tail",
        lambda dep, **kw: read_deployment_log_tail(
            dep, allowed_prefixes=(str(tmp_path),), **kw
        ),
    )
    monkeypatch.setattr(
        failure_snapshot,
        "read_deployment_log_tail",
        lambda dep, **kw: read_deployment_log_tail(
            dep, allowed_prefixes=(str(tmp_path),), **kw
        ),
    )

    log_file = tmp_path / "rollout-error.log"
    log_file.write_text(
        " Pulling worker ...\nfailed to copy: httpReadSeeker: unexpected EOF\n",
        encoding="utf-8",
    )
    record = {
        "deploymentId": "dep-rollout-77",
        "status": "error",
        "createdAt": "2026-10-28T03:39:31Z",
        "errorMessage": "compose failed",
        "logPath": str(log_file),
    }

    class FakeClient:
        def get_compose_deployments(self, compose_id):
            return [record]

        def get_compose(self, compose_id):
            return {
                "composeId": compose_id,
                "composeStatus": "error",
            }

    client = FakeClient()
    with pytest.raises(RuntimeError) as excinfo:
        wait_for_rollout(
            client,
            "cmp-staging-1",
            set(),
            timeout=5,
            interval=1,
            _sleep=lambda _s: None,
        )
    msg = str(excinfo.value)
    assert msg.startswith("deploy rollout entered error (compose cmp-staging-1)")
    assert "deploymentId=dep-rollout-77" in msg
    assert "errorMessage=compose failed" in msg
    assert f"logPath={log_file}" in msg
    assert "unexpected EOF" in msg

    snapshot = build_snapshot(client, "cmp-staging-1")
    assert snapshot["platform_failure_domain"] == "dokploy-deployment-error"
    assert snapshot["latest_deployment"]["deploymentId"] == "dep-rollout-77"
    assert snapshot["latest_deployment"]["errorMessage"] == "compose failed"
    assert snapshot["latest_deployment"]["logPath"] == str(log_file)
    assert "unexpected EOF" in snapshot["latest_deployment"]["logTail"]

    class NonDictClient:
        def get_compose(self, compose_id):
            return ["invalid"]

    bad_snapshot = build_snapshot(NonDictClient(), "cmp-bad")
    assert bad_snapshot["platform_failure_domain"] == "dokploy-api-unreachable"


_LABELS = (
    "infra2.vault.config.sha256=" + "c" * 64,
    "infra2.vault.template.sha256=" + "d" * 64,
)


def test_a_raw_stack_proves_its_identity_from_the_compose_dokploy_wrote(
    deployer, monkeypatch
):
    """#1822 (v3.1.144): a raw stack has no git checkout, so the #629 HEAD proof read
    'no readable HEAD' and failed a good deploy. A raw stack proves the written file."""
    monkeypatch.setattr(
        deployer, "_raw_identity_labels", classmethod(lambda cls: _LABELS)
    )
    written = "services:\n  vault-agent:\n    labels:\n" + "".join(
        f"    - {label}\n" for label in _LABELS
    )
    assert (
        deployer._verify_raw_identity(
            deployer_module._HostStack(
                "vps", "platform-app-abc123", lambda cmd: _Run(True, written)
            ),
            _LABELS,
        )
        is None
    )
    missing = deployer._verify_raw_identity(
        deployer_module._HostStack(
            "vps", "platform-app-abc123", lambda cmd: _Run(False, "", "No such file")
        ),
        _LABELS,
    )
    assert missing and "no raw compose file" in missing
    other = written.replace("d" * 64, "e" * 64)
    stale = deployer._verify_raw_identity(
        deployer_module._HostStack(
            "vps", "platform-app-abc123", lambda cmd: _Run(True, other)
        ),
        _LABELS,
    )
    assert stale and "lacks this deploy's identity" in stale and "template" in stale


def test_verify_checkout_identity_routes_a_raw_stack_and_keeps_head_for_github(
    deployer, monkeypatch
):
    seen: list[str] = []

    def run(cmd):
        seen.append(cmd)
        return _Run(True, "".join(f"- {label}\n" for label in _LABELS))

    stack = deployer_module._HostStack("vps", "platform-app-abc123", run)
    monkeypatch.setattr(
        deployer, "_raw_identity_labels", classmethod(lambda cls: _LABELS)
    )
    assert deployer._verify_checkout_identity(stack) is None
    assert seen and seen[0].startswith("cat ") and "code/docker-compose.yml" in seen[0]
    assert not any("rev-parse" in cmd for cmd in seen)

    seen.clear()
    monkeypatch.setattr(deployer, "_raw_identity_labels", classmethod(lambda cls: None))
    deployer._verify_checkout_identity(stack)
    assert any("rev-parse HEAD" in cmd for cmd in seen)


def test_the_real_todo_stack_is_raw_with_both_identity_labels() -> None:
    """The real platform/30.todo deployer takes the raw proof, with both labels."""
    todo = deployer_module.load_deployer_class("platform/todo")
    labels = todo._raw_identity_labels()
    assert labels is not None and len(labels) == 2
    assert {label.split("=")[0] for label in labels} == {
        "infra2.vault.config.sha256",
        "infra2.vault.template.sha256",
    }


def test_a_raw_stack_without_identity_labels_fails_closed(deployer) -> None:
    """Review of #1892: with no label, the file-exists check alone proves nothing."""
    stack = deployer_module._HostStack(
        "vps", "platform-app-abc123", lambda cmd: _Run(True, "services: {}\n")
    )
    message = deployer._verify_raw_identity(stack, ())
    assert message and "no identity label" in message


_RELEASE_HASH = "1" * 64
_LAST_HASH = "2" * 64


def _inspected(*containers: tuple[str, dict[str, str]]) -> str:
    """`docker inspect --format RAW_ENV_INSPECT_FORMAT` lines: name, space, env JSON."""
    import json

    return "".join(
        f"/{name} {json.dumps([f'{k}={v}' for k, v in env.items()])}\n"
        for name, env in containers
    )


def _raw(deployer, monkeypatch, *, recorded: str | None = _RELEASE_HASH):
    """Make the fixture deployer a raw stack whose compose env records `recorded`."""
    monkeypatch.setattr(
        deployer, "_raw_identity_labels", classmethod(lambda cls: _LABELS)
    )
    monkeypatch.setattr(
        deployer, "get_remote_config_hash", classmethod(lambda cls: recorded)
    )
    return deployer


def _raw_stack(answer):
    seen: list[str] = []

    def run(cmd):
        seen.append(cmd)
        return answer

    return deployer_module._HostStack("vps", "platform-app-abc123", run), seen


def test_a_raw_stack_binds_its_running_containers_to_this_release(
    deployer, monkeypatch
):
    """#1893: the file labels change only with the Vault config or template, so a
    dropped deploy that left last release's file matched. The running containers must
    carry the CONFIGURATION_SHA256 the compose env records."""
    _raw(deployer, monkeypatch)
    stack, seen = _raw_stack(
        _Run(
            True,
            _inspected(
                (
                    "platform-app",
                    {"PORT": "8000", "CONFIGURATION_SHA256": _RELEASE_HASH},
                ),
                ("platform-app-vault-agent", {"CONFIGURATION_SHA256": _RELEASE_HASH}),
                ("platform-app-sidecar", {"PORT": "1"}),
            ),
        )
    )
    assert deployer._verify_raw_release_binding(stack) is None
    assert len(seen) == 1
    assert (
        "docker ps -q --filter label=com.docker.compose.project=platform-app-abc123"
        in seen[0]
    )
    assert "docker inspect --format" in seen[0] and "json .Config.Env" in seen[0]


def test_a_raw_container_with_another_configuration_sha256_fails(deployer, monkeypatch):
    _raw(deployer, monkeypatch)
    stack, _seen = _raw_stack(
        _Run(
            True,
            _inspected(
                ("platform-app", {"CONFIGURATION_SHA256": _RELEASE_HASH}),
                ("platform-app-vault-agent", {"CONFIGURATION_SHA256": _LAST_HASH}),
            ),
        )
    )
    message = deployer._verify_raw_release_binding(stack)
    assert message and "run another release" in message
    assert f"platform-app-vault-agent={_LAST_HASH[:12]}" in message
    assert "platform-app=" not in message
    empty, _seen = _raw_stack(
        _Run(True, _inspected(("platform-app", {"CONFIGURATION_SHA256": ""})))
    )
    assert "platform-app=empty" in deployer._verify_raw_release_binding(empty)


def test_a_raw_stack_with_no_container_that_sets_configuration_sha256_fails_closed(
    deployer, monkeypatch
):
    _raw(deployer, monkeypatch)
    for listing in ("", _inspected(("platform-app", {"PORT": "8000"}))):
        stack, _seen = _raw_stack(_Run(True, listing))
        message = deployer._verify_raw_release_binding(stack)
        assert message and "sets CONFIGURATION_SHA256" in message
    _raw(deployer, monkeypatch, recorded=None)
    stack, seen = _raw_stack(_Run(True, "should not be read"))
    message = deployer._verify_raw_release_binding(stack)
    assert message and "records no CONFIGURATION_SHA256" in message and seen == []
    _raw(deployer, monkeypatch)
    monkeypatch.setattr(deployer, "IN_SERVICE_DEADLINE_SECONDS", 0)
    broken, _seen = _raw_stack(_Run(False, "", "permission denied"))
    with pytest.raises(deployer_module._StackUnobservable, match="permission denied"):
        deployer._verify_raw_release_binding(broken)


def test_verify_in_service_binds_a_raw_stack_after_its_containers_settle(
    deployer, monkeypatch
):
    """The post-deploy path: the file proof, the containers, then the release binding."""
    _raw(deployer, monkeypatch)
    written = "".join(f"- {label}\n" for label in _LABELS)
    healthy = _ps(
        ("backend", "running", "Up 9 seconds (healthy)"),
        ("frontend", "running", "Up 9 seconds (healthy)"),
        ("vault-agent", "running", "Up 9 seconds (healthy)"),
    )
    stale = _inspected(("platform-app", {"CONFIGURATION_SHA256": _LAST_HASH}))
    c = _Context([_Run(True, written), _Run(True, healthy), _Run(True, stale)])
    message = deployer.verify_in_service(c, "cid")
    assert message and "run another release" in message
    assert "docker inspect" in c.commands[2] and len(c.commands) == 3
    fresh = _inspected(("platform-app", {"CONFIGURATION_SHA256": _RELEASE_HASH}))
    c = _Context([_Run(True, written), _Run(True, healthy), _Run(True, fresh)])
    assert deployer.verify_in_service(c, "cid") is None
    assert not any("rev-parse" in cmd for cmd in c.commands)


def test_a_github_stack_keeps_its_proofs_without_the_release_binding(
    deployer, monkeypatch
):
    """#1893 is raw only: a github stack proves HEAD (#629) and #1247 as before."""
    monkeypatch.setattr(
        deployer,
        "_verify_raw_release_binding",
        classmethod(lambda cls, stack: pytest.fail("a github stack was bound")),
    )
    healthy = _ps(
        ("backend", "running", "Up 9 seconds (healthy)"),
        ("frontend", "running", "Up 9 seconds (healthy)"),
        ("vault-agent", "running", "Up 9 seconds (healthy)"),
    )
    c = _Context([_Run(True, "a" * 40 + "\n"), _Run(True, healthy)])
    assert deployer.verify_in_service(c, "cid") is None
    assert len(c.commands) == 2 and "rev-parse HEAD" in c.commands[0]
    monkeypatch.setattr(
        deployer,
        "_find_remote_compose",
        classmethod(lambda cls, e: {"env": f"IAC_DEPLOY_REF={'b' * 40}\n"}),
    )
    stack, seen = _raw_stack(_Run(True, "c" * 40 + "\n"))
    message = deployer._verify_recorded_deploy_ref_checkout(stack)
    assert message and "(#1247)" in message and "rev-parse HEAD" in seen[0]


def test_the_raw_skip_path_no_longer_returns_a_silent_none(deployer, monkeypatch):
    """#1893: `git rev-parse` always fails for a raw stack, so #1247 returned None on
    the verify_still_in_service skip path. A raw stack now takes the binding."""
    _raw(deployer, monkeypatch)
    stale, seen = _raw_stack(
        _Run(True, _inspected(("platform-app", {"CONFIGURATION_SHA256": _LAST_HASH})))
    )
    message = deployer._verify_recorded_deploy_ref_checkout(stale)
    assert message and "run another release" in message
    assert not any("rev-parse" in cmd for cmd in seen)
    unset, _seen = _raw_stack(_Run(True, _inspected(("platform-app", {"PORT": "1"}))))
    assert "sets CONFIGURATION_SHA256" in deployer._verify_recorded_deploy_ref_checkout(
        unset
    )
    fresh, _seen = _raw_stack(
        _Run(
            True, _inspected(("platform-app", {"CONFIGURATION_SHA256": _RELEASE_HASH}))
        )
    )
    assert deployer._verify_recorded_deploy_ref_checkout(fresh) is None


def test_the_real_todo_containers_set_configuration_sha256() -> None:
    """The binding needs a container that sets it: in the compiled todo compose, the
    todo app and its vault-agent sidecar both do."""
    import yaml

    todo = deployer_module.load_deployer_class("platform/todo")
    from common.deploy.compose_compile import load_stack

    services = yaml.safe_load(load_stack(todo.compose_path).compiled)["services"]
    carriers = {
        name
        for name, spec in services.items()
        if "CONFIGURATION_SHA256" in (spec.get("environment") or {})
    }
    assert carriers == {"todo", "vault-agent"}


# --- #1899 review: a one-off ssh failure is "not settled yet", not a verdict -------

_SSH_DOWN = "ssh: connect to host vps port 22: Connection timed out"
_ALL_HEALTHY = _ps(
    ("backend", "running", "Up 9 seconds (healthy)"),
    ("frontend", "running", "Up 9 seconds (healthy)"),
    ("vault-agent", "running", "Up 9 seconds (healthy)"),
)


def _clock(monkeypatch, *ticks: float) -> None:
    """time.monotonic answers `ticks` in order; one more call fails the test."""
    answers = iter(ticks)
    monkeypatch.setattr(deployer_module.time, "monotonic", lambda: next(answers))


def test_one_failed_docker_ps_then_a_good_read_passes(deployer):
    c = _Context(
        [
            _Run(True, "a" * 40 + "\n"),
            _Run(False, "", _SSH_DOWN),
            _Run(True, _ALL_HEALTHY),
        ]
    )
    assert deployer.verify_in_service(c, "cid") is None
    assert len(c.commands) == 3 and c.answers == []


def test_docker_ps_failing_until_the_deadline_raises_unobservable(
    deployer, monkeypatch
):
    """Deadline at 0 + 180 s: the read at 10 s retries, the read at 200 s gives up."""
    _clock(monkeypatch, 0.0, 10.0, 200.0)
    c = _Context([_Run(False, "", _SSH_DOWN), _Run(False, "", _SSH_DOWN)])
    with pytest.raises(
        deployer_module._StackUnobservable, match="Connection timed out"
    ):
        deployer.verify_still_in_service(c, "cid")
    assert len(c.commands) == 2 and c.answers == []


def test_one_failed_raw_docker_inspect_then_a_good_read_binds(deployer, monkeypatch):
    _raw(deployer, monkeypatch)
    answers = [
        _Run(False, "", _SSH_DOWN),
        _Run(
            True, _inspected(("platform-app", {"CONFIGURATION_SHA256": _RELEASE_HASH}))
        ),
    ]
    stack, seen = _raw_stack(None)
    stack = stack._replace(run=lambda cmd: seen.append(cmd) or answers.pop(0))
    assert deployer._verify_raw_release_binding(stack) is None
    assert len(seen) == 2 and answers == []


def test_raw_docker_inspect_failing_until_the_deadline_raises_unobservable(
    deployer, monkeypatch
):
    _raw(deployer, monkeypatch)
    _clock(monkeypatch, 0.0, 10.0, 200.0)
    stack, seen = _raw_stack(_Run(False, "", _SSH_DOWN))
    with pytest.raises(deployer_module._StackUnobservable, match="could not inspect"):
        deployer._verify_raw_release_binding(stack)
    assert len(seen) == 3


def test_a_compose_without_an_app_name_raises_at_once(deployer, monkeypatch):
    """Deterministic: no appName names no containers, so no read and no wait."""
    monkeypatch.setattr(
        "common.deploy.dokploy_client.get_dokploy",
        lambda host=None: SimpleNamespace(
            get_compose=lambda compose_id: {"appName": ""}
        ),
    )
    slept: list[float] = []
    monkeypatch.setattr(deployer_module.time, "sleep", slept.append)
    c = _Context([_Run(True, _ALL_HEALTHY)])
    with pytest.raises(deployer_module._StackUnobservable, match="reports no appName"):
        deployer.verify_still_in_service(c, "cid")
    assert c.commands == [] and slept == []
