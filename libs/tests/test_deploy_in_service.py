"""A deploy is not a success until the stack is in service (#629, #691, #698)."""

from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

import libs.deploy.deployer as deployer_module
from libs.deploy.in_service import (
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
    assert "prefect-worker" in prefect  # restart: on-failure, but a long-running worker
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
        "libs.dokploy.get_dokploy",
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
        NotInService, "verify_vault_app_token", classmethod(lambda cls: {"valid": True})
    )
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

REDIS_COMPOSE = """
services:
  redis:
    image: redis:7
    healthcheck: {test: ["CMD", "redis-cli", "ping"]}
"""
REDIS_UP = _ps(("redis", "running", "Up 3 days (healthy)"))
REDIS_EXITED = _ps(("redis", "exited", "Exited (137) 2 hours ago"))


class _Host:
    """The VPS over ssh: Dokploy's checkout HEAD, the stack's containers, and every
    running container by name (what the dependent restart looks at, #726)."""

    def __init__(
        self,
        checkout: str,
        containers: str,
        *,
        listable: bool = True,
        running: tuple[str, ...] = (),
        restart_ok: bool = True,
    ):
        self.checkout, self.containers, self.listable = checkout, containers, listable
        self.running, self.restart_ok = running, restart_ok
        self.commands: list[str] = []

    def run(self, cmd, hide=True, warn=True):
        self.commands.append(cmd)
        if "rev-parse HEAD" in cmd:
            return _Run(True, self.checkout + "\n")
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
    """platform/redis in `env`; any other lookup finds nothing."""

    def __init__(self, env: str = "production"):
        self.env = env

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        if (name, project_name, env_name) == ("redis", "platform", self.env):
            return {"composeId": "cid", "appName": "platform-redis-x1"}
        return None

    def get_compose(self, compose_id):
        assert compose_id == "cid"
        return {"composeId": "cid", "appName": "platform-redis-x1"}


def _redis_sync(
    monkeypatch, *, remote_hash: str, env: str = "production", suffix: str = ""
):
    """A platform/redis sync in `env` at RELEASE_SHA against a stack last deployed from
    LAST_DEPLOY_SHA; `remote_hash` "h1" means its config did not change. Returns the
    deployer and the list `composing` appends to when a (re)deploy is triggered."""
    from libs.service_identity import ServiceIdentity

    d = deployer_module
    identity = ServiceIdentity.build(
        "platform/redis", env, component="redis", service_name="redis"
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

    class Redis(d.Deployer):
        service = "redis"
        compose_path = "platform/02.redis/compose.yaml"
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
        def verify_vault_app_token(cls):
            return {"valid": True}

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
            return REDIS_COMPOSE

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
                runtime_hash=env["IAC_CONFIG_HASH"],
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

    monkeypatch.setattr("libs.dokploy.get_dokploy", lambda host=None: _Dokploy(env))
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setattr(d.time, "sleep", lambda _s: None)
    monkeypatch.setenv("IAC_DEPLOY_REF", RELEASE_SHA)
    monkeypatch.delenv("DEPLOY_ACTION", raising=False)
    return Redis, deployed


def test_an_unchanged_stack_at_an_older_checkout_is_skipped_not_restarted(
    monkeypatch,
):
    """#718 regression: the skip path asked the post-deploy identity proof, which
    wants Dokploy's checkout at THIS release; an unchanged stack is at its last
    deploy's, so every release would have restarted every unchanged service (a redis
    restart breaks OpenPanel and Authentik workers, #726)."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h1")
    host = _Host(checkout=LAST_DEPLOY_SHA, containers=REDIS_UP)

    result = redis.sync(host)

    assert result["action"] == "skipped", result
    assert deployed == []
    assert not host.asked_for_the_checkout
    assert any(
        "label=com.docker.compose.project=platform-redis-x1" in cmd
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


def test_an_unchanged_stack_on_an_unreachable_host_is_skipped(monkeypatch):
    """Unobservable is not dead: like an unreadable remote hash, a host that cannot
    list the containers skips (with a warning) instead of amplifying the load."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h1")
    warnings: list[str] = []
    monkeypatch.setattr(deployer_module, "warning", warnings.append)
    host = _Host(checkout=LAST_DEPLOY_SHA, containers="", listable=False)

    result = redis.sync(host)

    assert result["action"] == "skipped" and deployed == []
    assert any(
        "could not check the unchanged stack is in service" in w and "timed out" in w
        for w in warnings
    )


def test_a_changed_stack_still_fails_on_a_stale_checkout_after_deploy(monkeypatch):
    """#629 is untouched: after THIS release deployed the stack, a Dokploy checkout
    left at the previous release is a failed deploy, healthy containers or not."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h0")
    host = _Host(checkout=LAST_DEPLOY_SHA, containers=REDIS_UP)

    result = redis.sync(host)

    assert len(deployed) == 1
    assert result["action"] == "failed"
    assert "stale clone" in result["details"] and "v1.2.4" in result["details"]


def test_a_changed_stack_at_the_new_checkout_is_updated(monkeypatch):
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h0")
    host = _Host(checkout=RELEASE_SHA, containers=REDIS_UP)

    result = redis.sync(host)

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
REDIS = "platform/02.redis/compose.yaml"


def _everything_running(suffix: str) -> tuple[str, ...]:
    return tuple(
        _names(REDIS, suffix) + _names(OPENPANEL, suffix) + _names(AUTHENTIK, suffix)
    )


def test_a_skipped_redis_sync_restarts_no_dependent(monkeypatch):
    """#726: only a sync that recreated Redis flushes its script cache; a skip must
    not bounce OpenPanel and Authentik on every release (#718)."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h1")
    host = _Host(
        checkout=LAST_DEPLOY_SHA, containers=REDIS_UP, running=_everything_running("")
    )

    result = redis.sync(host)

    assert result["action"] == "skipped" and deployed == []
    assert "restarted_dependents" not in result
    assert host.restarted == []
    assert not any("{{.Names}}" in cmd for cmd in host.commands)


def test_an_applied_redis_sync_restarts_exactly_its_declared_dependents(monkeypatch):
    """#726: after Redis is redeployed and in service, OpenPanel api + worker and the
    Authentik worker are restarted in one call — and nothing else (not the OpenPanel
    dashboard, not the Authentik server, not Redis itself)."""
    redis, deployed = _redis_sync(monkeypatch, remote_hash="h0")
    printed: list[str] = []
    monkeypatch.setattr(deployer_module, "success", printed.append)
    host = _Host(
        checkout=RELEASE_SHA, containers=REDIS_UP, running=_everything_running("")
    )

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


def test_a_staging_redis_sync_restarts_only_staging_dependents(monkeypatch):
    """OpenPanel is prod_only (no staging instance), so a staging Redis restarts only
    Authentik's staging worker — never a production container."""
    redis, _deployed = _redis_sync(
        monkeypatch, remote_hash="h0", env="staging", suffix="-staging"
    )
    host = _Host(
        checkout=RELEASE_SHA,
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
    host = _Host(checkout=RELEASE_SHA, containers=REDIS_UP, running=running)

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
    host = _Host(
        checkout=RELEASE_SHA,
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
    # a service nothing depends on needs no host at all
    assert deployer_module.Deployer.restart_dependents(_Host(RELEASE_SHA, ""), {}) == []


def test_verify_in_service_still_reports_an_unlistable_host_as_a_failure(deployer):
    """The post-deploy proof keeps its fail-closed message; only the skip path treats
    an unobservable host as no evidence."""
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
