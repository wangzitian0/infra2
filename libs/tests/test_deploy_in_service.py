"""A deploy is not a success until the stack is in service (#629, #691, #698)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import libs.deploy.deployer as deployer_module
from libs.deploy.in_service import (
    DOCKER_PS_FORMAT,
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
