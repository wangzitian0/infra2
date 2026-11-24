"""Todo service deployment — the Canary Infrastructure Verification Tool."""

from __future__ import annotations

import io
import re
import sys

from invoke import task
from invoke.exceptions import Exit
from infra2_sdk.routing import CANARY_SLOT

from common.console import error, header, info, run_with_status, success
from common.deploy.deployer import Deployer, make_tasks
from common.core.facets import ProbeFacet, SecretsFacet, SignalFacet

shared_tasks = sys.modules.get("platform.30.todo.shared")


class TodoDeployer(Deployer):
    service = "todo"
    compose_path = "platform/30.todo/compose.yaml"
    data_path = ""

    subdomain = None
    service_port = 8000
    service_name = "todo"
    canary_slot = CANARY_SLOT
    redeploy_on_ref_change = True
    secret_key = ""

    backups = ()

    # The deploy issues OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES under this name
    # (Deployer.sync); the compose passes them to the container (#991).
    telemetry_service_name = "platform-todo"

    # The probe runner reads the status document over the Docker network (#991). The old
    # `Exemption(probes)` called the service "self-proving"; nothing read the status, and
    # staging answered 503 for hours unnoticed. Severity is `error` (P1): it shipped at
    # `warning` and was raised after the staging fault drill and a full green production
    # day (#991). The platform's own probes carry P0 for the core services, so an outage
    # of a dependency pages there first. The 15 s timeout exceeds the 8 s per-check
    # deadline in app.py.
    probes = (
        ProbeFacet(
            name="todo-canary-status",
            kind="http",
            target="http://platform-todo${ENV_SUFFIX}:8000/api/canary/status",
            expected="200",
            severity="error",
            timeout_seconds=15,
        ),
    )
    # Signal classification: a minute-tier alert debounced by the probe runner's shared
    # loop (DEFAULT_FAILURE_THRESHOLD=3, DEFAULT_RENOTIFY_SECONDS=0, tools/infra_probe_runner.py).
    signals = (
        SignalFacet(
            tier="minute",
            type="alert",
            consecutive_failures=3,
            renotify_window_sec=0,
        ),
    )

    # Vault self-refresh facts: the vault-agent renders the Redis password, DATABASE_URL
    # and the S3 canary key into the app container (AppRole auth).
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-todo-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-todo${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )

    # The one Postgres login of the Canary Todo. The Vault template composes DATABASE_URL
    # with this role (common/tests/test_preview_platform_render.py keeps them equal).
    TODO_PG_ROLE = "todo_app"
    TODO_PG_CONNECTION_LIMIT = 20
    # token_urlsafe(32), the SDK's generator for runtime secrets, yields only these characters.
    # A value outside this set is refused: it cannot be quoted into SQL or a URL without doubt.
    _TODO_PG_PASSWORD = re.compile(r"[A-Za-z0-9_-]{16,128}")

    @classmethod
    def compose_env_base(cls, env: dict | None = None) -> dict[str, str]:
        """Include OpenPanel client settings and browser OTLP endpoint alongside base env."""
        from common.core.environ import infra_domain
        from common.deploy.env_config import otel_env
        from common.observability.openpanel import openpanel_env

        e = env or cls.env()
        base = super().compose_env_base(e)
        domain = str(e.get("INTERNAL_DOMAIN") or infra_domain())
        base.update(openpanel_env(base["ENV"]))
        base.update(otel_env(domain=domain))
        return base

    @classmethod
    def todos_storage_sql(cls, password: str) -> str:
        """Idempotent SQL for the todos persistence role, table, enum, and minimal DML grants."""
        if not cls._TODO_PG_PASSWORD.fullmatch(password or ""):
            raise ValueError(
                "todo Postgres password must be 16-128 characters from [A-Za-z0-9_-]"
            )
        role = cls.TODO_PG_ROLE
        attributes = (
            "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            f"CONNECTION LIMIT {cls.TODO_PG_CONNECTION_LIMIT}"
        )
        return (
            "SET log_min_error_statement = 'panic';\n"
            "SET log_statement = 'none';\n"
            "SET log_min_duration_statement = -1;\n"
            "DO $do$\nBEGIN\n"
            f"  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{role}') THEN\n"
            f"    CREATE ROLE {role} {attributes};\n"
            "  END IF;\n"
            "END\n$do$;\n"
            f"ALTER ROLE {role} WITH {attributes} PASSWORD '{password}';\n"
            "CREATE TABLE IF NOT EXISTS todos (\n"
            "  id SERIAL PRIMARY KEY,\n"
            "  title TEXT NOT NULL,\n"
            "  completed BOOLEAN NOT NULL DEFAULT FALSE,\n"
            "  capability TEXT NOT NULL DEFAULT 'general',\n"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
            ");\n"
            "ALTER TABLE todos ADD COLUMN IF NOT EXISTS capability TEXT NOT NULL DEFAULT 'general';\n"
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE todos TO {role};\n"
            f"GRANT USAGE, SELECT ON SEQUENCE todos_id_seq TO {role};\n"
            "DO $enum$\nBEGIN\n"
            "  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'canary_todo_capability') THEN\n"
            "    CREATE TYPE canary_todo_capability AS ENUM "
            "('general', 'postgres', 'redis', 's3', 'llm', 'otel', 'sso');\n"
            "  END IF;\n"
            "END\n$enum$;\n"
            "ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS 'general';\n"
            "ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS 'postgres';\n"
            "ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS 'redis';\n"
            "ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS 's3';\n"
            "ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS 'llm';\n"
            "ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS 'otel';\n"
            "ALTER TYPE canary_todo_capability ADD VALUE IF NOT EXISTS 'sso';\n"
        )

    @staticmethod
    def redact_password(text: str, password: str) -> str:
        """Remove the password, and any `PASSWORD '...'` clause, from text that is printed.

        psql echoes the failing statement with a caret under the error, and that statement
        holds the password.
        """
        if password:
            text = text.replace(password, "<redacted>")
        return re.sub(r"(?i)(PASSWORD\s+)'[^']*'", r"\1<redacted>", text)

    @classmethod
    def _ensure_todo_postgres_role(cls, c, env: str | None = None) -> bool:
        """Create or refresh the todo_app role and the todos table in the platform Postgres.

        The SQL goes over stdin, so the password never appears in an argument list on the
        runner or on the VPS. Any failure returns False: the Canary cannot log in without
        the role, and a deploy must not report that as healthy.
        """
        e = cls.env()
        backend = cls.secrets_backend(env=env)
        try:
            password = backend.get("TODO_POSTGRES_PASSWORD")
        except Exception as exc:  # noqa: BLE001 - an unreadable store is a deploy failure
            error(
                f"{cls.service}: cannot read TODO_POSTGRES_PASSWORD from Vault: {exc}"
            )
            return False
        if not password:
            error(
                f"{cls.service}: Vault holds no TODO_POSTGRES_PASSWORD; run the secret supply first"
            )
            return False
        try:
            sql = cls.todos_storage_sql(password)
        except ValueError as exc:
            error(f"{cls.service}: {exc}")
            return False
        container = f"platform-postgres{e.get('ENV_SUFFIX') or ''}"
        result = c.run(
            f"ssh root@{e['VPS_HOST']} "
            f"'docker exec -i {container} psql -U postgres -v ON_ERROR_STOP=1 -q'",
            in_stream=io.StringIO(sql),
            hide=True,
            warn=True,
        )
        if result.failed:
            stderr_clean = cls.redact_password((result.stderr or "").strip(), password)
            error(
                f"{cls.service}: could not provision Postgres role {cls.TODO_PG_ROLE}: "
                f"{stderr_clean}"
            )
            return False
        info(f"{cls.service}: Postgres role {cls.TODO_PG_ROLE} is present and current")
        return True

    @classmethod
    def apply_secret_supply(cls, c, *, env=None) -> bool:
        """Write the secrets, then make Postgres agree with the generated password.

        Both `sync` and `pre_compose` call this on every run. The role is provisioned
        after the supply succeeds and before the containers start, so the canary never
        probes a role that does not exist yet.
        """
        if not super().apply_secret_supply(c, env=env):
            return False
        return cls._ensure_todo_postgres_role(c, env=env)


@task
def sso_setup(c):
    """Register Canary Todo in Authentik SSO (One-time or idempotent)"""
    from infra2_sdk.routing import derive_preview_coordinates
    from infra2_sdk.runtime.environment import resolve_environment_tier

    env = TodoDeployer.env()
    internal_domain = env.get("INTERNAL_DOMAIN")

    if not internal_domain:
        error("Missing INTERNAL_DOMAIN")
        raise Exit(code=1)

    tier = resolve_environment_tier(
        env.get("ENVIRONMENT") or env.get("ENV") or "production"
    )
    coords = derive_preview_coordinates("todo", tier, base_domain=internal_domain)
    suffix = coords.env_suffix
    domain_suffix = coords.env_domain_suffix
    todo_url = coords.preview_url
    internal_host = f"platform-todo{suffix}"
    app_name = f"Canary Todo{(' (' + domain_suffix.lstrip('-') + ')') if domain_suffix else ''}"
    app_slug = f"canary-todo{domain_suffix}"

    header("Todo SSO Setup", f"Registering {todo_url} in Authentik")

    res = run_with_status(
        c, "invoke authentik.shared.create-root-token", "Ensure Root Token"
    )
    if not res.ok:
        raise Exit(code=1)

    res = run_with_status(
        c, "invoke authentik.shared.setup-admin-group", "Ensure Admin Group"
    )
    if not res.ok:
        raise Exit(code=1)

    cmd = (
        f"invoke authentik.shared.create-proxy-app "
        f"--name='{app_name}' "
        f"--slug='{app_slug}' "
        f"--external-host='{todo_url}' "
        f"--internal-host='{internal_host}' "
        f"--port=8000"
    )
    res = run_with_status(c, cmd, "Create SSO Application")
    if not res.ok:
        raise Exit(code=1)

    success("Todo SSO setup complete")


if shared_tasks:
    _tasks = make_tasks(TodoDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
