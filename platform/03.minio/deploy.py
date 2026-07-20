"""MinIO deployment with vault-init

Root credentials pattern:
- Stored in 1Password for Web Console login (human access)
- Synced to Vault for vault-agent (machine access)
"""

import subprocess
import sys

from libs.deploy.deployer import Deployer, make_tasks
from libs.env import generate_password
from libs.console import header, success, error, warning, info, env_vars
from libs.service_facets import PublicRouteFacet, BackupFacet, ProbeFacet, SecretsFacet

shared_tasks = sys.modules.get("platform.03.minio.shared")


class MinioDeployer(Deployer):
    service = "minio"
    compose_path = "platform/03.minio/compose.yaml"
    data_path = "/data/platform/minio"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="minio_bucket_mirror",
            restore_command="mirror the selected off-host bucket snapshot back into MinIO.",
        ),
    )
    secret_key = "root_password"

    # Infra probes (#541): rendered into INFRA_PROBE_SPECS by platform/alerting.
    probes = (
        ProbeFacet(
            name="minio-internal-http",
            kind="http",
            target="http://platform-minio${ENV_SUFFIX}:9000/minio/health/live",
            expected="200",
        ),
    )

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth per #257/#259).
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-minio-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-minio${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )

    # Domain configuration for Dokploy (Console)
    subdomain = "minio"  # minio.{INTERNAL_DOMAIN} -> Console

    # Public route probed from inside (#543, #209 reversed).
    public_routes = (
        PublicRouteFacet(
            name="minio-public-route",
            path="/minio/health/live",
        ),
    )
    service_port = 9001  # MinIO Console port
    service_name = "minio"

    # 1Password item for root credentials
    OP_ITEM = "platform/minio/admin"

    @classmethod
    def ensure_runtime_secrets(cls, c=None) -> bool:
        """Ensure every field consumed by secrets.ctmpl exists in Vault."""
        vault_secrets = cls.secrets()
        root_user = vault_secrets.get("root_user") or "admin"
        root_password = vault_secrets.get("root_password")

        if not root_password:
            root_password = generate_password(32)
            warning("Generated new MinIO root password")

        if not vault_secrets.set("root_user", root_user):
            error("Failed to store root_user in Vault")
            return False
        info("Vault: root_user stored")

        if not vault_secrets.set("root_password", root_password):
            error("Failed to store root_password in Vault")
            return False
        info("Vault: root_password stored")
        return True

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and sync root credentials to both 1Password and Vault."""
        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        header(f"{cls.service} pre_compose", "Setting up root credentials")

        vault_secrets = cls.secrets()
        root_user = vault_secrets.get("root_user") or "admin"
        root_password = vault_secrets.get("root_password")

        if not cls.ensure_runtime_secrets(c):
            return None
        root_user = vault_secrets.get("root_user") or "admin"
        root_password = vault_secrets.get("root_password")

        # Store in 1Password (for Web Console login). Non-blocking: deployment continues if fails.
        op_item = cls.OP_ITEM
        if e.get("ENV") != "production":
            op_item = f"{cls.OP_ITEM}-{e.get('ENV')}"
        if not cls._sync_to_1password(root_user, root_password, op_item):
            warning(
                "1Password sync failed; continuing deployment. Web Console credentials may be "
                "out of sync until 1Password is updated."
            )

        # Return VAULT_ADDR for vault-init pattern
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        env_vars("DOKPLOY ENV (vault-init)", result)
        success("pre_compose complete")
        domain_suffix = e.get("ENV_DOMAIN_SUFFIX", "")
        info(
            f"MinIO Console: https://{cls.subdomain}{domain_suffix}.{e.get('INTERNAL_DOMAIN')}"
        )
        info(f"MinIO S3 API: https://s3{domain_suffix}.{e.get('INTERNAL_DOMAIN')}")
        info(f"Login: {root_user} / (password in 1Password)")
        return result

    @classmethod
    def composing(cls, c, env_vars: dict) -> str:
        """Deploy via Dokploy API and sync dual domains."""
        # Call parent to deploy
        compose_id = super().composing(c, env_vars)

        # Sync domains: minio=Console(9001), s3=API(9000)
        cls._sync_domains(compose_id)

        return compose_id

    @classmethod
    def _sync_domains(cls, compose_id: str):
        """Ensure domains exist: minio.{domain}=Console, s3.{domain}=API."""
        from libs.dokploy import get_dokploy

        e = cls.env()
        domain = e.get("INTERNAL_DOMAIN")
        if not domain:
            warning("INTERNAL_DOMAIN not set, skipping domain sync")
            return

        host = f"cloud.{domain}"
        client = get_dokploy(host=host)
        domain_suffix = e.get("ENV_DOMAIN_SUFFIX", "")

        desired_domains = [
            {
                "host": f"minio{domain_suffix}.{domain}",
                "port": 9001,
                "https": True,
            },  # Console
            {
                "host": f"s3{domain_suffix}.{domain}",
                "port": 9000,
                "https": True,
            },  # S3 API
        ]

        info(
            f"Ensuring domains: minio{domain_suffix}.{domain}->9001(Console), s3{domain_suffix}.{domain}->9000(API)"
        )
        result = client.ensure_domains(
            compose_id=compose_id,
            desired_domains=desired_domains,
            service_name=cls.service_name,
        )

        # Report results
        if result["conflicts"]:
            error("Domain conflicts detected! Please fix manually in Dokploy UI:")
            for c in result["conflicts"]:
                warning(
                    f"  {c['host']}: exists with port {c['existing_port']}, need port {c['desired_port']}"
                )

        if result["errors"]:
            for err in result["errors"]:
                warning(f"Domain error: {err}")

        if result["created"] > 0:
            success(
                f"Domains: created={result['created']}, skipped={result['skipped']}"
            )
            # Trigger redeploy to generate new compose with updated Traefik labels
            info("Redeploying to apply domain changes...")
            client.deploy_compose(compose_id)
            success("Redeploy triggered - domain labels will be updated")
        elif result["skipped"] > 0:
            info(f"Domains already configured (skipped={result['skipped']})")

    @classmethod
    def _sync_to_1password(cls, username: str, password: str, op_item: str) -> bool:
        """Sync root credentials to 1Password for Web Console access."""
        try:
            # Check if item exists
            check_result = subprocess.run(
                ["op", "item", "get", op_item, "--vault=Infra2", "--format=json"],
                capture_output=True,
                text=True,
            )

            if check_result.returncode == 0:
                # Update existing item
                subprocess.run(
                    [
                        "op",
                        "item",
                        "edit",
                        op_item,
                        "--vault=Infra2",
                        f"username={username}",
                        f"password={password}",
                    ],
                    capture_output=True,
                    check=True,
                )
                info("1Password: Updated existing item")
            else:
                # Create new item
                subprocess.run(
                    [
                        "op",
                        "item",
                        "create",
                        "--category=login",
                        f"--title={op_item}",
                        "--vault=Infra2",
                        f"username={username}",
                        f"password={password}",
                    ],
                    capture_output=True,
                    check=True,
                )
                success("1Password: Created new item")
            return True
        except subprocess.CalledProcessError as exc:
            warning(f"1Password sync failed: {exc}")
            info("You can manually create: op item create --category=login ...")
            return False


if shared_tasks:
    _tasks = make_tasks(MinioDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
