"""OpenPanel deployment with vault-init"""

import sys
import os
from libs.deployer import Deployer, make_tasks
from libs.common import with_env_suffix
from libs.console import success, warning, info, error
from libs.env import get_secrets

shared_tasks = sys.modules.get("platform.24.openpanel.shared")


class OpenPanelDeployer(Deployer):
    service = "openpanel"
    compose_path = "platform/24.openpanel/compose.yaml"
    data_path = "/data/platform/openpanel"
    # Analytics — single prod instance (model B), no staging copy.
    prod_only = True
    uid = "1000"
    gid = "1000"
    secret_key = "cookie_secret"

    # Domain configuration - None to use compose.yaml Traefik labels
    subdomain = None
    service_port = 3000
    service_name = "op-dashboard"

    @classmethod
    def ensure_runtime_secrets(cls, c=None) -> bool:
        """Ensure every Vault key referenced by secrets.ctmpl exists."""
        e = cls.env()
        env_name = e.get("ENV", "production")
        project = e.get("PROJECT", "platform")

        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)
        if not pg_secrets.get("root_password"):
            warning("Postgres password not found in Vault")
            return False
        if not redis_secrets.get("password"):
            warning("Redis password not found in Vault")
            return False

        openpanel_secrets = get_secrets(project, "openpanel", env_name)

        # On first deploy the `.../openpanel` path does not exist yet, and
        # VaultSecrets.get() raises VaultSecretNotFoundError instead of returning
        # None. Treat "path missing" as "secret absent" so the set() calls below
        # create the path idempotently.
        def _read(key):
            try:
                return openpanel_secrets.get(key)
            except openpanel_secrets.VaultSecretNotFoundError:
                return None

        cookie_secret = _read("cookie_secret")
        if not cookie_secret:
            import secrets as py_secrets

            # OpenPanel COOKIE_SECRET: opaque random session signing key.
            cookie_secret = py_secrets.token_hex(32)
            if not openpanel_secrets.set("cookie_secret", cookie_secret):
                error("Failed to store cookie_secret in Vault")
                return False
            warning("Generated new cookie_secret in Vault")
        else:
            info("cookie_secret exists in Vault")

        # Check or set resend_api_key placeholder
        resend_api_key = _read("resend_api_key")
        if not resend_api_key:
            if not openpanel_secrets.set("resend_api_key", "placeholder"):
                error("Failed to store placeholder resend_api_key in Vault")
                return False
            warning("Set placeholder resend_api_key in Vault")

        return True

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories, check dependencies, ensure secrets exist in Vault."""
        from libs.console import fatal

        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        env_name = e.get("ENV", "production")
        project = e.get("PROJECT", "platform")

        # Check Vault access
        if not os.getenv("VAULT_ROOT_TOKEN"):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for: 1) Reading postgres/redis passwords, 2) Storing openpanel secrets\n"
                "   Get token: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' "
                "(or /Token; item: bootstrap/vault/Root Token)\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>",
            )

        # Check dependencies exist in Vault
        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)

        if not pg_secrets.get("root_password"):
            fatal(
                "Postgres password not found in Vault",
                "Run: export VAULT_ROOT_TOKEN=<token> && invoke postgres.setup",
            )
        if not redis_secrets.get("password"):
            fatal(
                "Redis password not found in Vault",
                "Run: export VAULT_ROOT_TOKEN=<token> && invoke redis.setup",
            )
        success("Verified postgres and redis secrets in Vault")

        # Create Postgres database (idempotent)
        postgres_container = with_env_suffix("platform-postgres", e)
        result = c.run(
            f"ssh root@{e['VPS_HOST']} \"docker exec {postgres_container} psql -U postgres -c 'CREATE DATABASE openpanel;'\"",
            warn=True,
            hide=True,
        )
        if result.failed:
            stderr = result.stderr or ""
            if "already exists" in stderr:
                info("Database 'openpanel' already exists in Postgres")
            else:
                fatal(
                    "Failed to create 'openpanel' database in Postgres",
                    f"Error: {stderr}",
                )
        else:
            success("Database 'openpanel' created in Postgres")

        # ClickHouse: OpenPanel runs its own dedicated, version-matched
        # ClickHouse (op-ch in compose.yaml) because the shared
        # platform-clickhouse is pinned to 25.5 by SigNoz while OpenPanel v2
        # requires 25.10. The op-ch container self-creates the `openpanel`
        # database via clickhouse/init-db.sh, so no provisioning is needed here.
        #
        # op-ch data lives on a durable host bind mount (${DATA_PATH}/op-ch) rather
        # than a Dokploy named volume, which gets recreated with a new hash on
        # redeploy and silently wipes the event schema. Create the dir with
        # ClickHouse ownership (uid/gid 101) before compose so the server can write.
        data_path = cls.data_path_for_env(e)
        ch_dir_result = c.run(
            f"ssh root@{e['VPS_HOST']} 'mkdir -p {data_path}/op-ch && chown -R 101:101 {data_path}/op-ch'",
            warn=True,
            hide=True,
        )
        if ch_dir_result.failed:
            fatal(
                "Failed to prepare durable op-ch data dir",
                f"Path: {data_path}/op-ch\nError: {ch_dir_result.stderr or ''}",
            )
        success(f"Prepared durable op-ch data dir at {data_path}/op-ch (uid/gid 101)")

        if not cls.ensure_runtime_secrets(c):
            fatal("Failed to ensure OpenPanel runtime secrets")

        # Return VAULT_ADDR for vault-init pattern
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result

    @classmethod
    def post_compose(cls, c, shared_tasks):
        """Verify deployment"""
        from libs.console import header, success, error

        header(f"{cls.service} post_compose", "Verifying")
        result = shared_tasks.status(c)
        if result["is_ready"]:
            success(f"post_compose complete - {result['details']}")
            return True
        error("Verification failed", result["details"])
        return False


if shared_tasks:
    _tasks = make_tasks(OpenPanelDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
