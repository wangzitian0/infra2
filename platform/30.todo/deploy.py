"""Todo service deployment — the Canary Infrastructure Verification Tool."""

from __future__ import annotations

import io
import re
import sys

from invoke import task

from libs.console import error, header, info, run_with_status, success
from libs.deploy.deployer import Deployer, make_tasks
from libs.core.facets import ProbeFacet, SecretsFacet, SignalFacet

shared_tasks = sys.modules.get("platform.30.todo.shared")


class TodoDeployer(Deployer):
    service = "todo"
    compose_path = "platform/30.todo/compose.yaml"
    data_path = ""

    subdomain = None
    service_port = 8000
    service_name = "todo"
    deploy_v2_canary = False
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

    # Vault self-refresh facts: the vault-agent renders the Redis password, the Postgres
    # canary password and the S3 canary key into the app container (AppRole auth).
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-todo-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-todo${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )

    # The dedicated Postgres login of the canary. The compose names the same role in
    # CANARY_POSTGRES_USER (libs/tests/test_todo_canary_depth.py keeps them equal).
    CANARY_PG_ROLE = "canary_ro"
    CANARY_PG_CONNECTION_LIMIT = 5
    # token_urlsafe(32), the SDK's generator for runtime secrets, yields only these characters.
    # A value outside this set is refused: it cannot be quoted into SQL without doubt.
    _CANARY_PG_PASSWORD = re.compile(r"[A-Za-z0-9_-]{16,128}")

    @classmethod
    def canary_role_sql(cls, password: str) -> str:
        """Idempotent SQL for the canary role: login only, no grants, read-only sessions.

        `SELECT 1` needs CONNECT, which PUBLIC already holds, so the role gets no GRANT.
        ALTER ROLE runs on every call, so a rotated Vault password takes effect.
        """
        if not cls._CANARY_PG_PASSWORD.fullmatch(password or ""):
            raise ValueError(
                "canary Postgres password must be 16-128 characters from [A-Za-z0-9_-]"
            )
        role = cls.CANARY_PG_ROLE
        attributes = (
            "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            f"CONNECTION LIMIT {cls.CANARY_PG_CONNECTION_LIMIT}"
        )
        return (
            # The server logs a failed statement with its text by default, and the
            # ALTER ROLE below carries the password. The session turns that logging off first.
            "SET log_min_error_statement = 'panic';\n"
            "SET log_statement = 'none';\n"
            "SET log_min_duration_statement = -1;\n"
            "DO $do$\nBEGIN\n"
            f"  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{role}') THEN\n"
            f"    CREATE ROLE {role} {attributes};\n"
            "  END IF;\n"
            "END\n$do$;\n"
            f"ALTER ROLE {role} WITH {attributes} PASSWORD '{password}';\n"
            f"ALTER ROLE {role} SET default_transaction_read_only = on;\n"
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
    def _ensure_canary_postgres_role(cls, c, env: str | None = None) -> bool:
        """Create or refresh the canary role in the environment's platform Postgres.

        The SQL goes over stdin, so the password never appears in an argument list on the
        runner or on the VPS. Any failure returns False: the canary cannot log in without
        the role, and a deploy must not report that as healthy.
        """
        e = cls.env()
        try:
            password = cls.secrets_backend(env=env).get("CANARY_POSTGRES_PASSWORD")
        except Exception as exc:  # noqa: BLE001 - an unreadable store is a deploy failure
            error(
                f"{cls.service}: cannot read CANARY_POSTGRES_PASSWORD from Vault: {exc}"
            )
            return False
        if not password:
            error(
                f"{cls.service}: Vault holds no CANARY_POSTGRES_PASSWORD; run the secret supply first"
            )
            return False
        try:
            sql = cls.canary_role_sql(password)
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
            error(
                f"{cls.service}: could not provision Postgres role {cls.CANARY_PG_ROLE}: "
                f"{cls.redact_password((result.stderr or '').strip(), password)}"
            )
            return False
        info(
            f"{cls.service}: Postgres role {cls.CANARY_PG_ROLE} is present and current"
        )
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
        return cls._ensure_canary_postgres_role(c, env=env)


@task
def sso_setup(c):
    """Register Canary Todo in Authentik SSO (One-time or idempotent)"""
    env = TodoDeployer.env()
    suffix = env.get("ENV_SUFFIX") or ""
    domain_suffix = env.get("ENV_DOMAIN_SUFFIX") or ""
    internal_domain = env.get("INTERNAL_DOMAIN")

    if not internal_domain:
        error("Missing INTERNAL_DOMAIN")
        return

    todo_url = f"https://todo{domain_suffix}.{internal_domain}"
    internal_host = f"platform-todo{suffix}"
    app_name = f"Canary Todo{(' (' + domain_suffix.lstrip('-') + ')') if domain_suffix else ''}"
    app_slug = f"canary-todo{domain_suffix}"

    header("Todo SSO Setup", f"Registering {todo_url} in Authentik")

    res = run_with_status(
        c, "invoke authentik.shared.create-root-token", "Ensure Root Token"
    )
    if not res.ok:
        return

    res = run_with_status(
        c, "invoke authentik.shared.setup-admin-group", "Ensure Admin Group"
    )
    if not res.ok:
        return

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
        return

    success("Todo SSO setup complete")


if shared_tasks:
    _tasks = make_tasks(TodoDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
