"""Todo service deployment — the Canary Infrastructure Verification Tool."""

from __future__ import annotations

import io
import re
import sys
import time

from pathlib import Path

from invoke import task
from invoke.exceptions import Exit
from common.deploy.routing import CANARY_SLOT

from common.console import error, header, info, run_with_status, success
from common.deploy.canary_pin import (
    CanarySlot,
    canary_todo_facets,
    load_canary_slots,
    primary_canary_slot,
)
from common.deploy.deployer import Deployer, make_tasks
from common.core.facets import SignalFacet

shared_tasks = sys.modules.get("platform.30.todo.shared")


def _active_slots() -> tuple[CanarySlot, ...]:
    return load_canary_slots()


class TodoDeployer(Deployer):
    service = "todo"
    compose_path = "platform/30.todo/compose.yaml"
    data_path = ""

    subdomain = None
    service_port = 8000
    canary_slot = CANARY_SLOT
    redeploy_on_ref_change = True
    secret_key = ""

    backups = ()

    # Each slot emits OTEL_SERVICE_NAME=platform-todo-<slot> and SERVICE_VERSION=vX.Y.Z.
    telemetry_service_name = "platform-todo"

    signals = (
        SignalFacet(
            tier="minute",
            type="alert",
            consecutive_failures=3,
            renotify_window_sec=0,
        ),
    )

    TODO_PG_ROLE = "todo_app"
    # Staging v3.2.22 (run 45754381410): one platform batch recreated platform-postgres and
    # then ran this psql before the new container accepted it, so the deploy failed. The
    # SQL is idempotent, so a failed run gets another try; 6 tries 5 s apart cover a
    # Postgres restart. The last failure still fails the deploy.
    PG_ATTEMPTS = 6
    PG_RETRY_SECONDS = 5.0
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
    def todos_storage_sql(
        cls,
        password: str,
        *,
        slots: tuple[CanarySlot, ...] | None = None,
    ) -> str:
        """Idempotent SQL for slot databases, roles, schema types, and minimal DML grants."""
        if not cls._TODO_PG_PASSWORD.fullmatch(password or ""):
            raise ValueError(
                "todo Postgres password must be 16-128 characters from [A-Za-z0-9_-]"
            )
        active = _active_slots() if slots is None else slots
        attributes = (
            "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            f"CONNECTION LIMIT {cls.TODO_PG_CONNECTION_LIMIT}"
        )
        parts: list[str] = [
            "SET log_min_error_statement = 'panic';\n",
            "SET log_statement = 'none';\n",
            "SET log_min_duration_statement = -1;\n",
        ]
        targets = [(cls.TODO_PG_ROLE, "postgres")] + [
            (s.pg_role, s.database) for s in active
        ]
        for role, db in targets:
            parts.append(
                "DO $do$\nBEGIN\n"
                f"  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{role}') THEN\n"
                f"    CREATE ROLE {role} {attributes};\n"
                "  END IF;\n"
                "END\n$do$;\n"
                f"ALTER ROLE {role} WITH {attributes} PASSWORD '{password}';\n"
            )
            if db != "postgres":
                parts.append(
                    f"SELECT 'CREATE DATABASE {db} OWNER {role}' "
                    f"WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '{db}')\\gexec\n"
                )
        for role, db in targets:
            if db != "postgres":
                parts.append(f"\\connect {db}\n")
            parts.append(
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
                "CREATE TABLE IF NOT EXISTS todos (\n"
                "  id SERIAL PRIMARY KEY,\n"
                "  title TEXT NOT NULL,\n"
                "  completed BOOLEAN NOT NULL DEFAULT FALSE,\n"
                "  capability canary_todo_capability NOT NULL DEFAULT 'general',\n"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
                ");\n"
                "ALTER TABLE todos ADD COLUMN IF NOT EXISTS capability canary_todo_capability NOT NULL DEFAULT 'general';\n"
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE todos TO {role};\n"
                f"GRANT USAGE, SELECT ON SEQUENCE todos_id_seq TO {role};\n"
            )
            if db != "postgres":
                parts.append("\\connect postgres\n")
        return "".join(parts)

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
        for attempt in range(1, cls.PG_ATTEMPTS + 1):
            result = c.run(
                f"ssh root@{e['VPS_HOST']} "
                f"'docker exec -i {container} psql -U postgres -v ON_ERROR_STOP=1 -q'",
                in_stream=io.StringIO(sql),
                hide=True,
                warn=True,
            )
            if not result.failed or attempt == cls.PG_ATTEMPTS:
                break
            info(
                f"{cls.service}: psql in {container} failed (attempt {attempt} of "
                f"{cls.PG_ATTEMPTS}); trying again in {cls.PG_RETRY_SECONDS:g} s"
            )
            time.sleep(cls.PG_RETRY_SECONDS)
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


TodoDeployer.service_name, TodoDeployer.probes, TodoDeployer.secrets = (
    canary_todo_facets(Path(__file__).resolve().parents[2])
)


@task
def sso_setup(c):
    """Register Canary Todo in Authentik SSO (One-time or idempotent)"""
    from common.deploy.routing import derive_preview_coordinates
    from infra2_sdk.config import resolve_tier

    env = TodoDeployer.env()
    internal_domain = env.get("INTERNAL_DOMAIN")

    if not internal_domain:
        error("Missing INTERNAL_DOMAIN")
        raise Exit(code=1)

    tier = resolve_tier(env.get("ENVIRONMENT") or env.get("ENV") or "production")
    coords = derive_preview_coordinates("todo", tier, base_domain=internal_domain)
    suffix = coords.env_suffix
    domain_suffix = coords.env_domain_suffix
    todo_url = coords.preview_url
    primary = primary_canary_slot()
    internal_host = f"{primary.container_prefix}{suffix}"
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
