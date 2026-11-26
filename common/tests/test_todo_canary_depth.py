"""Canary depth for platform/30.todo (#991): the deploy-declaration side.

The app side of these checks moved to infra2-sdk canary/todo/tests/test_canary_depth.py (#247).

The canary exists to prove the platform's real paths. Before #991 it passed on `-NOAUTH`,
on a Postgres `E` reply and on S3 liveness, nobody read its status, and it sent no
telemetry. These tests pin the opposite behaviour:

1. Redis needs a real AUTH, then SETEX/GET/DEL of a random value. NOAUTH fails.
2. Postgres needs a real login as a dedicated role and `SELECT 1`. An `E` reply fails.
3. S3 needs PUT, GET and DELETE of one object under a canary prefix.
4. The probe runner consumes `/api/canary/status` through a ProbeFacet.
5. Only `/api/health` is routed without SSO.
6. The service configures OpenTelemetry from the identity the deploy issues.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from pathlib import Path

from common.tests.conftest import load_repo_module
from urllib.parse import urlsplit

import pytest

from common.security import registry as secrets_registry
from common.core import registry as reg
from common.observability.probe_specs import render_probe_spec_text
from common.tests.ambient_env import clear_telemetry_env
from common.deploy.canary_pin import canary_sdk_version
from common.tests.compose_env import compose_services, resolve
from common.tests.docker_host import DockerHost
from common.tests.traefik_rules import routers_from_labels, serving_router


ROOT = Path(__file__).resolve().parents[2]


SERVICE_DIR = ROOT / "platform/30.todo"


COMPOSE = SERVICE_DIR / "compose.yaml"


# Credentials are generated per run. A committed literal would trip secret scanners and
# would prove nothing: no test depends on a particular value.
PG_PASSWORD = secrets.token_urlsafe(24)


def _load(name: str, path: Path):
    return load_repo_module(path, name)


def _todo_template() -> str:
    """The Vault template that the deploy compiles into the todo sidecar (#1822)."""
    from common.deploy.compose_compile import vault_template_for

    template = vault_template_for(
        "platform/30.todo/compose.yaml", COMPOSE.read_text(encoding="utf-8")
    )
    assert template, "the todo stack sets x-infra2.vault"
    return template


@pytest.fixture(autouse=True)
def _no_ambient_telemetry_env(monkeypatch):
    """The app starts telemetry while it loads. The shell of a developer must not decide
    whether it loads (#1209)."""
    clear_telemetry_env(monkeypatch)


@pytest.fixture()
def deploy():
    return _load("todo_deploy_depth", SERVICE_DIR / "deploy.py")


def test_todo_declares_a_probe_on_the_internal_canary_status(deploy) -> None:
    attrs = reg.service_attrs()["platform/todo"]
    assert not any(e.check_id == "probes" for e in attrs.exemptions), (
        "the probes exemption said 'self-proving'; nothing read the status (#991)"
    )
    by_name = {probe.name: probe for probe in attrs.probes}
    assert set(by_name) == {"todo-canary-status"}
    probe = by_name["todo-canary-status"]
    assert probe.kind == "http"
    assert probe.expected == "200"
    assert probe.severity == "error", (
        "P1 since the staging drill and a full green production day (#991)"
    )
    assert probe.depends_on == ""
    assert probe.service_id == ""


def test_probe_renders_into_the_probe_runner_specs() -> None:
    lines = [
        line
        for line in render_probe_spec_text().splitlines()
        if line.startswith("todo-canary-status|")
    ]
    assert lines == [
        "todo-canary-status|http|http://platform-todo${ENV_SUFFIX}:8000/api/canary/status"
        "|200|error|15||platform/todo"
    ]


def test_todo_signal_is_a_debounced_minute_alert() -> None:
    from common.observability.signal_entries import render_internal_signal_entries

    entries = [
        entry
        for entry in render_internal_signal_entries()
        if entry["signal"] == "todo-canary-status"
    ]
    assert {entry["environment"] for entry in entries} == {"production", "staging"}
    for entry in entries:
        assert entry["service_id"] == "platform/todo"
        assert entry["component"] == "todo"
        assert entry["severity"] == "error"
        assert entry["tier"] == "minute" and entry["type"] == "alert"
        assert entry["consecutive_failures"] == 3
        assert entry["renotify_window_sec"] == 0


def test_public_router_does_not_name_the_canary_status() -> None:
    """A second guard on the label itself: the public rule must not mention the path."""
    labels = compose_services(COMPOSE)["todo"]["labels"]
    public_rules = [
        label
        for label in labels
        if "-public" in label and label.split("=", 1)[0].endswith(".rule")
    ]
    assert len(public_rules) == 1
    assert "canary" not in public_rules[0]


def test_todo_is_registered_with_the_secret_registry_and_templates_are_generated() -> (
    None
):
    from common.security import secrets_render

    service = secrets_registry.lookup("platform", "todo")
    assert service is not None
    assert service.directory == "platform/30.todo"
    assert secrets_render.check((service,)) == []


def test_todo_manifest_sources_each_secret_from_the_right_class() -> None:
    manifest = secrets_registry.merged_manifest(
        secrets_registry.lookup("platform", "todo")
    )
    fields = {field.env: field for field in manifest.fields}

    redis = fields["REDIS_URL"]
    assert redis.source == "runtime"
    assert redis.provided_by == "platform/redis:password"
    assert redis.sensitive
    assert (
        redis.composed_from
        == "redis://:{password}@platform-redis{env:ENV_SUFFIX}:6379/0"
    )

    # One field: Vault key TODO_POSTGRES_PASSWORD holds the generated password (the
    # store_key); only the composed DATABASE_URL enters the process (#1823 N2).
    assert "TODO_POSTGRES_PASSWORD" not in fields
    url = fields["DATABASE_URL"]
    assert url.source == "runtime" and url.sensitive and not url.empty_ok
    assert not url.provided_by, "a generated value, not the Postgres superuser password"
    assert url.store_backed and url.store_key == "TODO_POSTGRES_PASSWORD"
    assert url.composed_keys == ("TODO_POSTGRES_PASSWORD",)
    template = _todo_template()
    assert "TODO_POSTGRES_PASSWORD=" not in template
    assert ".Data.data.TODO_POSTGRES_PASSWORD" in template

    removed = {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "CANARY_POSTGRES_USER",
        "CANARY_POSTGRES_DATABASE",
        "CANARY_POSTGRES_PASSWORD",
        "TODO_POSTGRES_USER",
        "TODO_POSTGRES_DATABASE",
    }
    assert removed.isdisjoint(fields)
    assert all(not field.aliases for field in manifest.fields)

    for env in ("S3_ACCESS_KEY", "S3_SECRET_KEY"):
        assert fields[env].source == "human"
        assert fields[env].empty_ok and fields[env].sensitive
        assert not fields[env].store_key, "the Vault key is the canonical env name"

    from infra2_sdk.config import check_shared_env

    # Every todo field name conforms to shared_env.yaml: no exemption.
    assert check_shared_env(fields) == []
    assert any(
        "DATABASE_URL" in issue for issue in check_shared_env([*fields, "database_url"])
    )


def test_todo_policy_never_reads_the_superuser_secrets() -> None:
    """The canary is reachable over SSO from the internet. It must not be able to read
    the Postgres or S3 root credentials, only its own path and the Redis password."""
    policy = (SERVICE_DIR / "vault-policy.hcl").read_text(encoding="utf-8")
    paths = set(re.findall(r'path "([^"]+)"', policy))
    assert "secret/data/platform/{{env}}/todo" in paths
    assert "secret/data/platform/{{env}}/redis" in paths
    for forbidden in ("postgres", "minio", "s3", "alerting"):
        assert not any(forbidden in path for path in paths), (forbidden, paths)


def test_compose_runs_a_vault_agent_and_mounts_its_secrets_read_only() -> None:
    services = compose_services(COMPOSE)
    assert set(services) == {"vault-agent", "todo"}
    todo = services["todo"]
    assert "secrets:/secrets:ro" in todo["volumes"]
    assert "vault-agent" in todo["depends_on"]
    assert "REDIS_PASSWORD" not in todo["environment"], (
        "the password comes from the rendered file, never from compose env"
    )
    entrypoint = " ".join(todo["entrypoint"])
    assert ". /secrets/.env" in entrypoint and 'exec "$$@"' in entrypoint
    assert todo["command"] == ["python", "/app/app.py"]
    agent = services["vault-agent"]
    assert agent["container_name"] == "platform-todo-vault-agent${ENV_SUFFIX}"
    assert agent.get("mem_limit"), "a new container may not add resource-limit debt"
    # The template is inline (VAULT_SECRETS_TEMPLATE), not a mount from the checkout.
    assert agent["volumes"] == ["secrets:/vault/secrets"]
    assert agent["environment"]["VAULT_SECRETS_TEMPLATE"]


def test_secrets_facet_names_the_compose_containers() -> None:
    attrs = reg.service_attrs()["platform/todo"]
    (facet,) = attrs.secrets
    services = compose_services(COMPOSE)
    assert facet.auth_method == "approle"
    assert facet.vault_agent_container == services["vault-agent"]["container_name"]
    assert facet.app_containers == (services["todo"]["container_name"],)


def test_todo_role_name_is_one_fact_across_deployer_and_database_url(deploy) -> None:
    manifest = secrets_registry.merged_manifest(
        secrets_registry.lookup("platform", "todo")
    )
    (url,) = [field for field in manifest.fields if field.env == "DATABASE_URL"]
    role = deploy.TodoDeployer.TODO_PG_ROLE
    assert url.composed_from == (
        f"postgresql://{role}:{{TODO_POSTGRES_PASSWORD}}"
        "@platform-postgres{env:ENV_SUFFIX}:5432/postgres"
    )


def test_the_canary_ro_login_is_gone_and_the_prune_removes_its_vault_key(
    deploy,
) -> None:
    deployer = deploy.TodoDeployer
    for name in (
        "CANARY_PG_ROLE",
        "canary_role_sql",
        "_ensure_canary_" + "postgres_role",
    ):
        assert not hasattr(deployer, name), name
    env = compose_services(COMPOSE)["todo"]["environment"]
    assert [name for name in env if "POSTGRES" in name] == []
    template = _todo_template()
    assert "CANARY_POSTGRES_PASSWORD" not in template
    # The supply prune deletes a Vault key that the registry does not list. No entry
    # keeps the key of the removed login (#1823).
    service = secrets_registry.lookup("platform", "todo")
    assert service.store_only_keys == ()
    kept = secrets_registry.store_keys(service)
    assert "CANARY_POSTGRES_PASSWORD" not in kept
    assert "TODO_POSTGRES_PASSWORD" in kept, "the key behind DATABASE_URL stays"


class _Run:
    def __init__(self, ok=True, stdout="", stderr=""):
        self.ok, self.failed = ok, not ok
        self.stdout, self.stderr = stdout, stderr


class _Host:
    def __init__(self, ok=True, stderr=""):
        self.commands: list[str] = []
        self.stdin: list[str] = []
        self._ok, self._stderr = ok, stderr

    def run(self, command, in_stream=None, hide=True, warn=True, **kwargs):
        self.commands.append(command)
        self.stdin.append(in_stream.read() if in_stream is not None else "")
        return _Run(self._ok, stderr=self._stderr)


class _Store:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def _stub_role_env(monkeypatch, deployer, store_values) -> None:
    monkeypatch.setattr(
        deployer,
        "env",
        classmethod(
            lambda cls: {
                "ENV": "staging",
                "ENV_SUFFIX": "-staging",
                "VPS_HOST": "vps.example",
            }
        ),
    )
    monkeypatch.setattr(
        deployer,
        "secrets_backend",
        classmethod(lambda cls, env=None: _Store(store_values)),
    )


def test_todos_storage_sql_is_idempotent_and_reapplies_the_password(deploy) -> None:
    sql = deploy.TodoDeployer.todos_storage_sql(PG_PASSWORD)
    normalized = " ".join(sql.split())
    create_at = normalized.index("CREATE ROLE")
    guard_at = normalized.index("IF NOT EXISTS")
    assert guard_at < create_at, "CREATE ROLE must sit behind an existence guard"
    assert (
        "ALTER ROLE todo_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
        in normalized
    )
    assert f"PASSWORD '{PG_PASSWORD}'" in normalized, (
        "a rotated password must be re-applied"
    )
    assert "CONNECTION LIMIT 20" in normalized
    assert "canary_ro" not in normalized
    assert "DROP" not in normalized


@pytest.mark.parametrize(
    "password",
    [
        "",
        "short",
        "has'quote0123456789",
        "semi;colon0123456789",
        "back\\slash0123456789",
        "dollar$$0123456789",
    ],
)
def test_todos_storage_sql_rejects_passwords_it_cannot_quote_safely(
    deploy, password
) -> None:
    with pytest.raises(ValueError):
        deploy.TodoDeployer.todos_storage_sql(password)


def test_ensure_role_runs_the_sql_over_stdin_so_the_password_never_hits_argv(
    deploy, monkeypatch
) -> None:
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"TODO_POSTGRES_PASSWORD": PG_PASSWORD})
    host = _Host()

    assert deployer._ensure_todo_postgres_role(host) is True

    (command,) = host.commands
    assert command.startswith("ssh root@vps.example ")
    assert "docker exec -i platform-postgres-staging psql -U postgres" in command
    assert "ON_ERROR_STOP=1" in command
    assert PG_PASSWORD not in command
    assert PG_PASSWORD in host.stdin[0]
    assert host.stdin[0] == deployer.todos_storage_sql(PG_PASSWORD)


def test_ensure_role_fails_closed_when_the_password_is_not_in_vault(
    deploy, monkeypatch
) -> None:
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {})
    host = _Host()
    assert deployer._ensure_todo_postgres_role(host) is False
    assert host.commands == []


def test_ensure_role_fails_closed_when_psql_fails(deploy, monkeypatch) -> None:
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"TODO_POSTGRES_PASSWORD": PG_PASSWORD})
    monkeypatch.setattr(deployer, "PG_RETRY_SECONDS", 0)
    host = _Host(ok=False, stderr="psql: error: connection refused")
    assert deployer._ensure_todo_postgres_role(host) is False
    assert len(host.commands) == deployer.PG_ATTEMPTS == 6


class _FlakyHost(_Host):
    """psql fails ``failures`` times, then succeeds: Postgres still starting."""

    def __init__(self, failures):
        super().__init__()
        self._failures = failures

    def run(self, command, in_stream=None, hide=True, warn=True, **kwargs):
        self.commands.append(command)
        self.stdin.append(in_stream.read() if in_stream is not None else "")
        ok = len(self.commands) > self._failures
        return _Run(ok, stderr="" if ok else "")


def test_ensure_role_waits_for_a_postgres_that_the_same_batch_recreated(
    deploy, monkeypatch
) -> None:
    """Replay of staging v3.2.22: psql failed with an empty stderr right after the
    platform batch recreated platform-postgres. The next tries succeed.

    ### Mutation evidence
    - One try only (``PG_ATTEMPTS = 1``, or no loop) fails this test.
    """
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"TODO_POSTGRES_PASSWORD": PG_PASSWORD})
    slept: list[float] = []
    monkeypatch.setattr(deploy.time, "sleep", slept.append)
    host = _FlakyHost(failures=2)

    assert deployer._ensure_todo_postgres_role(host) is True

    assert len(host.commands) == 3
    assert len(set(host.commands)) == 1, "every try runs the same command"
    assert host.stdin == [deployer.todos_storage_sql(PG_PASSWORD)] * 3
    assert slept == [deployer.PG_RETRY_SECONDS] * 2
    assert deployer.PG_ATTEMPTS * deployer.PG_RETRY_SECONDS <= 30


def test_supply_hook_provisions_the_role_only_after_the_supply_succeeded(
    deploy, monkeypatch
) -> None:
    from common.deploy.deployer import Deployer

    deployer = deploy.TodoDeployer
    order: list[str] = []

    def supply(cls, c, *, env=None):
        order.append("supply")
        return supply.result

    supply.result = True
    monkeypatch.setattr(Deployer, "apply_secret_supply", classmethod(supply))
    monkeypatch.setattr(
        deployer,
        "_ensure_todo_postgres_role",
        classmethod(lambda cls, c, env=None: order.append("role") or True),
    )

    assert deployer.apply_secret_supply(object(), env="staging") is True
    assert order == ["supply", "role"]

    order.clear()
    supply.result = False
    assert deployer.apply_secret_supply(object(), env="staging") is False
    assert order == ["supply"], "no database change while the supply is incomplete"


def test_supply_hook_fails_when_the_role_cannot_be_provisioned(
    deploy, monkeypatch
) -> None:
    from common.deploy.deployer import Deployer

    deployer = deploy.TodoDeployer
    monkeypatch.setattr(
        Deployer, "apply_secret_supply", classmethod(lambda cls, c, *, env=None: True)
    )
    monkeypatch.setattr(
        deployer,
        "_ensure_todo_postgres_role",
        classmethod(lambda cls, c, env=None: False),
    )
    assert deployer.apply_secret_supply(object(), env="staging") is False


def test_first_sync_creates_the_role_although_no_consumer_exists_yet(
    deploy, monkeypatch
) -> None:
    """The first deploy: the supply writes the generated password and asks for a restart of
    a vault-agent and an app container that do not exist. The sync must still reach the
    role creation (#991 audit: it failed on `docker restart` first)."""
    from common.security import supply as supply_module

    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"TODO_POSTGRES_PASSWORD": PG_PASSWORD})

    def apply(service, env_name, restart=None, resolver=None):
        restart(("TODO_POSTGRES_PASSWORD",))
        from types import SimpleNamespace

        return SimpleNamespace(
            ok=True, notes=(), missing=(), summary=lambda: "generated the password"
        )

    monkeypatch.setattr(supply_module, "apply", apply)
    host = DockerHost(containers={"platform-postgres-staging"})

    assert deployer.apply_secret_supply(host, env="staging") is True

    assert host.restart_commands == []
    psql = [
        (command, stdin)
        for command, stdin in zip(host.commands, host.stdin)
        if "psql" in DockerHost.remote(command)
    ]
    assert len(psql) == 1, "the role is created on the first sync"
    assert psql[0][1] == deployer.todos_storage_sql(PG_PASSWORD)


def test_role_sql_keeps_the_password_out_of_the_server_log(deploy) -> None:
    """A failed ALTER ROLE is logged with its statement text by default. The session turns
    statement logging off before the password statement runs."""
    sql = " ".join(deploy.TodoDeployer.todos_storage_sql(PG_PASSWORD).split())
    alter = sql.index("ALTER ROLE todo_app WITH")
    for setting in (
        "SET log_min_error_statement = 'panic';",
        "SET log_statement = 'none';",
        "SET log_min_duration_statement = -1;",
    ):
        assert setting in sql, setting
        assert sql.index(setting) < alter, f"{setting} must come before the password"
    assert sql.index("SET log_min_error_statement") < sql.index("DO $do$")


def test_a_failed_role_sync_never_prints_the_password(deploy, monkeypatch) -> None:
    """psql prints the failing statement with a caret under the error, and that statement
    holds the password."""
    deployer = deploy.TodoDeployer
    _stub_role_env(monkeypatch, deployer, {"TODO_POSTGRES_PASSWORD": PG_PASSWORD})
    stderr = (
        'psql:<stdin>:12: ERROR:  syntax error at or near "x"\n'
        f"LINE 1: ALTER ROLE todo_app WITH LOGIN PASSWORD '{PG_PASSWORD}';\n"
        "                                                  ^\n"
        f"DETAIL: rejected {PG_PASSWORD}"
    )
    monkeypatch.setattr(deployer, "PG_RETRY_SECONDS", 0)
    host = DockerHost(other_ok=False, other_stderr=stderr)
    printed: list[str] = []
    monkeypatch.setattr(
        deploy,
        "error",
        lambda *args, **kwargs: printed.append(" ".join(map(str, args))),
    )

    assert deployer._ensure_todo_postgres_role(host) is False

    assert printed, "the failure must be reported"
    assert all(PG_PASSWORD not in line for line in printed), printed
    assert any("<redacted>" in line for line in printed), printed
    assert any("syntax error" in line for line in printed), "the cause stays readable"


def test_redaction_removes_any_password_clause_not_only_the_known_value(deploy) -> None:
    redact = deploy.TodoDeployer.redact_password
    text = "x PASSWORD 'some-other-value' y PASSWORD  'and-this' z known-secret-1"
    cleaned = redact(text, "known-secret-1")
    assert "some-other-value" not in cleaned and "and-this" not in cleaned
    assert "known-secret-1" not in cleaned
    assert cleaned.count("<redacted>") == 3


def test_todos_storage_sql_provisions_todo_app_role_and_table(deploy) -> None:
    sql = deploy.TodoDeployer.todos_storage_sql(PG_PASSWORD)
    normalized = " ".join(sql.split())
    assert "CREATE ROLE todo_app LOGIN NOSUPERUSER" in normalized
    assert "ALTER ROLE todo_app WITH LOGIN NOSUPERUSER" in normalized
    assert f"PASSWORD '{PG_PASSWORD}'" in normalized
    assert "CREATE TYPE canary_todo_capability AS ENUM" in normalized
    assert "CREATE TABLE IF NOT EXISTS todos" in normalized
    assert normalized.index(
        "CREATE TYPE canary_todo_capability AS ENUM"
    ) < normalized.index("CREATE TABLE IF NOT EXISTS todos")
    assert "capability canary_todo_capability NOT NULL DEFAULT 'general'" in normalized
    assert (
        "ALTER TABLE todos ADD COLUMN IF NOT EXISTS capability canary_todo_capability NOT NULL DEFAULT 'general'"
        in normalized
    )
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE todos TO todo_app" in normalized
    )
    assert "GRANT USAGE, SELECT ON SEQUENCE todos_id_seq TO todo_app" in normalized
    for label in ("general", "postgres", "redis", "s3", "llm", "otel", "sso"):
        assert f"'{label}'" in normalized
        assert (
            f"ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS '{label}'"
            in normalized
        )
    assert "SUPERUSER" not in normalized.replace("NOSUPERUSER", "")
    assert "default_transaction_read_only" not in normalized


def test_todo_deployer_compose_env_base_issues_openpanel_and_browser_otel(
    deploy,
) -> None:
    env = deploy.TodoDeployer.compose_env_base(
        {"ENV": "staging", "ENV_SUFFIX": "-staging", "INTERNAL_DOMAIN": "zitian.party"}
    )
    assert env["OPENPANEL_CLIENT_ID"]
    assert env["OPENPANEL_ENVIRONMENT"] == "staging"
    assert (
        env["NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT"]
        == "https://otel.zitian.party/v1/traces"
    )


def test_todo_compose_enables_all_four_app_peer_capabilities() -> None:
    import yaml
    from common.deploy.compose_compile import load_stack

    doc = yaml.safe_load(load_stack("platform/30.todo/compose.yaml").compiled)
    env = doc["services"]["todo"]["environment"]
    for key in (
        "S3_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OPENPANEL_HEALTH_URL",
        "OPENPANEL_CLIENT_ID",
        "OPENPANEL_API_URL",
        "OPENPANEL_ENVIRONMENT",
        "OPENPANEL_SCRIPT_URL",
        "NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT",
        "NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT",
        "NEXT_PUBLIC_GIT_SHA",
    ):
        assert key in env, key


def test_todo_runtime_hooks_manifest_declares_all_five_slots(tmp_path) -> None:
    """#1503, #1554, #1608, #1823: platform/30.todo/runtime-hooks.manifest.json is a schema 3
    RuntimeHooksManifest. Each of the 5 train slots fills its own cells, and each check
    delegates to probe_health.py."""
    from common.deploy.app_contract import (
        HOOK_SLOTS,
        execute_app_hook_slot,
        load_hooks_manifest,
        schema3_empty_cells,
    )
    from infra2_sdk.delivery import resolve_check_cells

    manifest = load_hooks_manifest(SERVICE_DIR / "runtime-hooks.manifest.json")
    assert manifest.schema_version == 3
    assert manifest.service == "platform/todo"
    checks = {check.id: check for check in manifest.checks}
    assert set(checks) == set(HOOK_SLOTS)
    assert {cid: resolve_check_cells(check) for cid, check in checks.items()} == {
        "smoke": ("preview.operation", "staging.operation", "prod.operation"),
        "preview_gate": ("preview.post",),
        "staging_gate": ("staging.post",),
        "prod_gate": ("prod.post",),
        "scheduled_audit": ("scheduled_audit",),
    }
    assert {cid: check.timeout_seconds for cid, check in checks.items()} == {
        "smoke": 60,
        "preview_gate": 120,
        "staging_gate": 180,
        "prod_gate": 120,
        "scheduled_audit": 300,
    }
    for slot in HOOK_SLOTS:
        assert schema3_empty_cells(manifest, slot) == (), slot
    for cid, check in checks.items():
        command = check.argv[-1]
        assert "python3 probe_health.py" in command, cid
        assert "urllib.request.urlopen" not in command, cid
        assert "EXPECTED_SHA" not in command, cid
    assert "--require-service platform/todo" in checks["staging_gate"].argv[-1]
    for gate in ("staging_gate", "prod_gate"):
        command = checks[gate].argv[-1]
        assert command.endswith(
            ' --gate "${APP_VERSION_REF:?APP_VERSION_REF is not set}"'
        ), gate
        # An empty ref stops the shell before probe_health runs (#1853 review).
        empty = subprocess.run(
            ["sh", "-c", command.replace("python3 probe_health.py", "echo")],
            env={
                "PATH": "/usr/bin:/bin",
                "APP_URL": "https://t",
                "APP_VERSION_REF": "",
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert empty.returncode != 0 and "APP_VERSION_REF is not set" in empty.stderr, (
            gate
        )
    assert "--gate" not in checks["smoke"].argv[-1]
    assert (SERVICE_DIR / "probe_health.py").is_file()

    for slot in HOOK_SLOTS:
        seen: list[dict[str, str]] = []

        def runner(cmd, **kw):
            seen.append(dict(kw.get("env") or {}))
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        res = execute_app_hook_slot(
            manifest,
            slot,
            app_url="https://todo-staging.example.com",
            environment="staging",
            version_ref="v1.0.0",
            cwd=tmp_path,
            ambient_env={"PATH": "/usr/bin:/bin"},
            runner=runner,
        )
        assert res.status == "pass"
        assert seen, slot
        assert seen[-1]["APP_VERSION_REF"] == "v1.0.0", slot


def test_the_compiled_template_label_replaces_the_mount_fingerprints(deploy) -> None:
    """#1822 Slice 2: the compiled sidecar holds the template inline and labels it with
    its SHA-256. The #1503 mount fingerprints of vault-agent.hcl and secrets.ctmpl have
    no reader left, so the deploy no longer issues them."""
    import hashlib

    base = deploy.TodoDeployer.compose_env_base(
        {"ENV": "staging", "ENV_SUFFIX": "-staging", "INTERNAL_DOMAIN": "example.com"}
    )
    assert not [k for k in base if k.startswith("IAC_MOUNT_SHA256_")]
    assert not hasattr(deploy.TodoDeployer, "rendered_mount_fingerprints")
    agent = compose_services(COMPOSE)["vault-agent"]
    assert not [label for label in agent["labels"] if "infra2.mount." in label]
    # The label is the SHA-256 of the template that the manifest renders.
    want = hashlib.sha256(_todo_template().encode()).hexdigest()
    assert f"infra2.vault.template.sha256={want}" in agent["labels"]


def test_probe_health_gate_fails_on_red_canary_and_sha_mismatch(
    monkeypatch,
) -> None:
    """AC3 (#1635): gate mode fails on a red verdict, a missing verdict and a SHA mismatch."""
    from types import SimpleNamespace

    probe = _load("probe_health_gate", SERVICE_DIR / "probe_health.py")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.strip()
    monkeypatch.chdir(ROOT)
    pinned = canary_sdk_version()
    assert probe.pinned_canary_sdk("HEAD") == pinned
    base = {
        "ok": True,
        "service": "platform/todo",
        "git_sha": sha,
        "sdk_version": pinned,
    }

    def run(body: dict, *flags: str) -> int:
        monkeypatch.setattr(
            probe,
            "poll_until_healthy",
            lambda *a, **k: SimpleNamespace(body=json.dumps(body)),
        )
        return probe.main(["https://x.example", *flags])

    good = {**base, "canary": {"ok": True, "checked_at": _now()}}
    assert run(good, "--gate", "HEAD") == 0
    for bad in (
        {**base, "canary": {"ok": False, "checked_at": _now()}},
        dict(base),
        {**good, "git_sha": "0" * 40},
        # the container must run the SDK that the pin names (infra2-sdk#247)
        {**good, "sdk_version": "0.0.0"},
        {key: value for key, value in good.items() if key != "sdk_version"},
    ):
        with pytest.raises(AssertionError):
            run(bad, "--gate", "HEAD")
    assert (
        run({**base, "canary": {"ok": False, "checked_at": "t"}}) == 0
    )  # no gate: status only


def test_probe_health_gate_waits_for_a_verdict_not_a_pass(monkeypatch) -> None:
    """#1635: gate mode polls until a verdict exists; a red verdict ends the wait at once."""
    from types import SimpleNamespace

    probe = _load("probe_health_wait", SERVICE_DIR / "probe_health.py")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.strip()
    monkeypatch.chdir(ROOT)
    sleeps: list[float] = []
    clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(probe.time, "sleep", fake_sleep)
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    def serve(canaries: list) -> None:
        calls = iter(canaries)
        monkeypatch.setattr(
            probe,
            "poll_until_healthy",
            lambda *a, **k: SimpleNamespace(
                body=json.dumps(
                    {
                        "ok": True,
                        "git_sha": sha,
                        "sdk_version": canary_sdk_version(),
                        "canary": next(calls, canaries[-1]),
                    }
                )
            ),
        )

    pending = {"ok": False, "checked_at": None}
    green = {"ok": True, "checked_at": _now()}
    red = {"ok": False, "checked_at": _now()}

    serve([pending, pending, green])
    assert probe.main(["https://x.example", "--gate", "HEAD"]) == 0
    assert sleeps == [10, 10]

    sleeps.clear()
    serve([red])
    with pytest.raises(AssertionError):
        probe.main(["https://x.example", "--gate", "HEAD"])
    assert sleeps == []

    serve([pending])
    with pytest.raises(AssertionError, match="no canary verdict after 90s"):
        probe.main(["https://x.example", "--gate", "HEAD"])
    assert sum(sleeps) >= 90


def _now(offset: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def test_probe_health_gate_refuses_stale_verdict_and_missing_field_without_wait(
    monkeypatch,
) -> None:
    """#1635: a verdict older than 180 s fails; a missing `canary` field fails with no wait."""
    from types import SimpleNamespace

    probe = _load("probe_health_stale", SERVICE_DIR / "probe_health.py")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.strip()
    monkeypatch.chdir(ROOT)
    sleeps: list[float] = []
    monkeypatch.setattr(probe.time, "sleep", sleeps.append)

    def run(canary) -> int:
        body = {"ok": True, "git_sha": sha, "sdk_version": canary_sdk_version()}
        if canary is not None:
            body["canary"] = canary
        monkeypatch.setattr(
            probe,
            "poll_until_healthy",
            lambda *a, **k: SimpleNamespace(body=json.dumps(body)),
        )
        return probe.main(["https://x.example", "--gate", "HEAD"])

    assert run({"ok": True, "checked_at": _now(-60)}) == 0
    with pytest.raises(AssertionError, match="stale"):
        run({"ok": True, "checked_at": _now(-400)})
    with pytest.raises(AssertionError):
        run(None)
    assert sleeps == []


# ------------------------------------------------------- deploy side of the app contract
# The app side of these contracts lives in infra2-sdk canary/todo/tests/test_canary_depth.py
# (#247). Both sides pin the same values; the staging canary_verify run proves them end to end.
SERVED_PATHS = {
    "/",
    "/index.html",
    "/api/health",
    "/api/canary/status",
    "/api/todos",
    "/api/todos/{todo_id}",
    "/api/todos/summary",
    "/api/auth/me",
}


def test_the_probe_interval_is_the_sixty_seconds_the_canary_cache_assumes() -> None:
    alerting = compose_services(ROOT / "platform/12.alerting/compose.yaml")
    raw = alerting["infra-probe-runner"]["environment"]["INFRA_PROBE_INTERVAL_SECONDS"]
    assert float(resolve(raw, {})) == 60.0


def test_the_probe_timeout_is_the_fifteen_seconds_the_canary_deadline_assumes() -> None:
    facet = reg.service_attrs()["platform/todo"].probes[0]
    assert facet.timeout_seconds == 15


def test_probe_target_matches_the_compose_container_port_and_app_route() -> None:
    probe = reg.service_attrs()["platform/todo"].probes[0]
    target = urlsplit(probe.target.replace("${ENV_SUFFIX}", "-staging"))
    service = compose_services(COMPOSE)["todo"]
    assert target.scheme == "http", "the probe stays on the Docker network"
    assert target.hostname == service["container_name"].replace(
        "${ENV_SUFFIX}", "-staging"
    )
    assert target.port == int(resolve(service["environment"]["PORT"], {}))
    assert target.path == "/api/canary/status"
    assert target.path in SERVED_PATHS


@pytest.mark.parametrize(
    ("domain_suffix", "env_suffix"), [("", ""), ("-staging", "-staging")]
)
def test_only_the_health_path_is_served_without_sso(domain_suffix, env_suffix) -> None:
    """Resolve the real routing decision for every path the app serves (Traefik rule
    language and priority order, not substring checks)."""
    service = compose_services(COMPOSE)["todo"]
    issued = {
        "ENV_DOMAIN_SUFFIX": domain_suffix,
        "ENV_SUFFIX": env_suffix,
        "INTERNAL_DOMAIN": "zitian.party",
    }
    labels = [resolve(label, issued) for label in service["labels"]]
    routers = routers_from_labels(labels)
    host = f"todo{domain_suffix}.zitian.party"

    forward_auth = {
        label.split("=", 1)[0].split(".")[3]
        for label in labels
        if ".forwardauth.address=" in label
    }
    assert forward_auth, "the SSO middleware must be declared"

    candidates = sorted(
        SERVED_PATHS
        | {
            "/api/todos/1",
            "/api/canary/status/",
            "/api/canary/status/extra",
            "/api/health/extra",
            "/api/healthz",
            "/api/canary",
        }
    )
    unauthenticated = set()
    for path in candidates:
        router = serving_router(routers, host=host, path=path)
        assert router is not None, (
            f"no router serves {path}; the SSO router must catch all"
        )
        if not any(m.split("@")[0] in forward_auth for m in router.middlewares):
            unauthenticated.add(path)
    assert unauthenticated == {"/api/health"}


def test_the_probe_runner_sees_the_gap_in_its_observed_text() -> None:
    """The runner keeps the first 128 bytes of the body. The canary status document puts
    its classification first (infra2-sdk canary/todo pins that side)."""
    from common.observability.probes import ProbeSpec, run_probe

    body = (
        '{"ok":false,"failed":[],"unconfigured":["s3"],'
        '"gating":["postgres","redis","s3"],"checks":{}}'
    )
    spec = ProbeSpec(
        name="todo-canary-status",
        kind="http",
        target="http://127.0.0.1/api/canary/status",
        expected="200",
        severity="warning",
        timeout_seconds=5,
    )
    result = run_probe(spec, http_get=lambda _url, _timeout: (503, body[:128]))
    assert result.ok is False, "the gap keeps the probe red"
    assert '"unconfigured":["s3"]' in result.observed
    assert '"failed":[]' in result.observed
    assert '"ok":false' in result.observed


def test_probe_health_gate_fails_closed_for_a_release_without_a_pin(
    monkeypatch,
) -> None:
    """Missing Canary SDK pin must fail closed in --gate mode, never skip."""
    from types import SimpleNamespace

    probe = _load("probe_health_gate_no_pin", SERVICE_DIR / "probe_health.py")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.strip()
    monkeypatch.chdir(ROOT)
    body = {"ok": True, "git_sha": sha, "canary": {"ok": True, "checked_at": _now()}}
    monkeypatch.setattr(
        probe,
        "poll_until_healthy",
        lambda *a, **k: SimpleNamespace(body=json.dumps(body)),
    )
    with pytest.raises(AssertionError, match="no Canary SDK pin found"):
        probe.main(["https://x.example", "--gate", "HEAD"], pin_reader=lambda _ref: "")
    with pytest.raises(AssertionError, match="pinned Canary SDK 9.9.9"):
        probe.main(
            [
                "https://x.example",
                "--gate",
                "HEAD",
            ],
            pin_reader=lambda _ref: "9.9.9",
        )
    monkeypatch.setattr(
        probe,
        "poll_until_healthy",
        lambda *a, **k: SimpleNamespace(
            body=json.dumps(
                {
                    **body,
                    "sdk_version": "9.9.9",
                    "canary": {"ok": True, "checked_at": _now(120)},
                }
            )
        ),
    )
    with pytest.raises(AssertionError, match="stale or unreadable"):
        probe.main(
            ["https://x.example", "--gate", "HEAD"], pin_reader=lambda _ref: "9.9.9"
        )
    monkeypatch.setattr(
        probe,
        "poll_until_healthy",
        lambda *a, **k: SimpleNamespace(body="not-json"),
    )
    with pytest.raises(AssertionError, match="invalid JSON"):
        probe.main(
            ["https://x.example", "--gate", "HEAD"], pin_reader=lambda _ref: "9.9.9"
        )


def test_probe_health_retries_transient_http_zero_and_fails_when_exhausted(
    monkeypatch,
) -> None:
    """Transient HTTP 0 timeouts retry up to HEALTH_POLL_MAX_ATTEMPTS within smoke budget."""
    probe = _load("probe_health_retry", SERVICE_DIR / "probe_health.py")
    assert probe.HEALTH_POLL_MAX_ATTEMPTS == 3
    worst_case_seconds = (
        probe.HEALTH_POLL_MAX_ATTEMPTS * probe.GATE_REQUEST_TIMEOUT_SECONDS
        + (probe.HEALTH_POLL_MAX_ATTEMPTS - 1) * probe.HEALTH_POLL_RETRY_SECONDS
    )
    assert worst_case_seconds < 60

    calls = 0

    def _transient_get(timeout: float = 10.0):
        def _get(_url: str) -> tuple[int, str]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 0, "timed out"
            return 200, json.dumps({"ok": True, "service": "platform/todo"})

        return _get

    monkeypatch.setattr(probe, "default_http_get", _transient_get)
    assert (
        probe.main(
            [
                "https://x.example",
                "--require-service",
                "platform/todo",
                "--retry-interval",
                "0",
            ]
        )
        == 0
    )
    assert calls == 2

    calls = 0

    def _always_zero_get(timeout: float = 10.0):
        def _get(_url: str) -> tuple[int, str]:
            nonlocal calls
            calls += 1
            return 0, "timed out"

        return _get

    monkeypatch.setattr(probe, "default_http_get", _always_zero_get)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        probe.main(["https://x.example", "--retry-interval", "0"])
    assert calls == 3

    with pytest.raises(SystemExit):
        probe.main(["https://x.example", "--attempts", "0"])
    with pytest.raises(SystemExit):
        probe.main(["https://x.example", "--retry-interval", "-1"])
