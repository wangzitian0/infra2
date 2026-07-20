import shutil
import sys

from libs.common import get_env
from libs.console import header, info, success, warning
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import SecretsFacet

shared_tasks = sys.modules.get("truealpha.10.app.shared")


class AppDeployer(Deployer):
    """TrueAlpha Application Deployer (Next.js web + FastAPI llm-service)."""

    service = "app"
    compose_path = "truealpha/truealpha/10.app/compose.yaml"
    data_path = None
    secret_key = "DATABASE_URL"
    project = "truealpha"  # Dokploy project name

    # The compose owns explicit Traefik routes (truealpha[-env].<domain>), so no
    # Dokploy-managed domain here (domain routing policy: never both).
    subdomain = None
    # TrueAlpha is an independent product with its own registered domain, not a
    # shared-platform service — override the shared INTERNAL_DOMAIN entirely so
    # its compose's ${INTERNAL_DOMAIN} Traefik Host() rules resolve under
    # truealpha.club instead of zitian.party. Read by
    # libs.app_deploy_request.make_plan via libs.service_registry.domain_for_service.
    domain = "truealpha.club"
    service_port = 3000
    service_name = "web"

    # Vault self-refresh facts (#542): the audit inventory derives from these
    # (AppRole auth from day one). Two surfaces, mirroring finance_report/app:
    #   1. the app itself;
    #   2. the multi-alias ephemeral PREVIEW surface (#522, generalized off
    #      finance_report/preview's pattern) — declared with an explicit
    #      service_id, borrowing the SOURCE env's app secrets
    #      (PREVIEW_SECRET_ENV, default staging).
    # NOTE: production rollout is _APP_COMPOSE_OVERRIDES-gated (see
    # libs/deploy_env_config.py). The prod compose_id is registered (live since
    # #547), so the vault audit covers this app AND its preview surface in
    # production — the "not yet in production" derivation dropped both
    # automatically when the compose_id landed.
    secrets = (
        SecretsFacet(
            vault_agent_container="truealpha-app-vault-agent${ENV_SUFFIX}",
            app_containers=(
                "truealpha-llm${ENV_SUFFIX}",
                "truealpha-web${ENV_SUFFIX}",
            ),
            auth_method="approle",
        ),
        SecretsFacet(
            service_id="truealpha/preview",
            compose_path="truealpha/truealpha/preview/compose.yaml",
            vault_agent_container="truealpha-app-vault-agent${ENV_SUFFIX}",
            app_containers=(
                "truealpha-llm${ENV_SUFFIX}",
                "truealpha-web${ENV_SUFFIX}",
            ),
            auth_method="approle",
        ),
    )

    @classmethod
    def pre_compose(cls, c) -> dict | None:
        env_vars = super().pre_compose(c)
        if env_vars is None:
            return None
        # Raw-archive bucket (truealpha runtime contract: immutable source
        # bytes live in S3-compatible storage; Postgres raw.fetches keeps
        # checksums + pointers). Same pattern as finance_report's bucket.
        cls._ensure_minio_bucket(c)
        return env_vars

    @classmethod
    def ensure_runtime_secrets(cls, c=None) -> bool:
        """Sync-path S3 readiness check.

        The iac-runner's sync NEVER calls pre_compose (it builds env straight
        from compose_env_base), so _ensure_minio_bucket above only runs in the
        manual pre-compose/setup tasks — a real deploy would silently ship an
        app with no S3 credentials. This hook IS called by sync; it cannot
        provision (no docker CLI in the runner) but it can refuse to be silent."""
        if not super().ensure_runtime_secrets(c):
            return False
        secrets = cls.secrets()
        if not (secrets.get("S3_ACCESS_KEY") and secrets.get("S3_SECRET_KEY")):
            warning("S3 credentials missing in Vault — the raw archive is not provisioned for this env")
            info(
                "Run ONCE on the VPS host: VAULT_TOKEN=... bash "
                "truealpha/truealpha/10.app/provision_bucket.sh <staging|production>, "
                "then restart the app vault-agent."
            )
        return True

    @classmethod
    def _ensure_minio_bucket(cls, c):
        minio_shared = sys.modules.get("platform.03.minio.shared")
        if not minio_shared:
            warning("MinIO shared tasks module not loaded; skipping bucket creation")
            return
        create_app_bucket = getattr(minio_shared, "create_app_bucket", None)
        if not create_app_bucket:
            warning("MinIO shared task create_app_bucket not found; skipping bucket creation")
            return

        secrets = cls.secrets()
        bucket_name = secrets.get("S3_BUCKET") or "truealpha-raw"
        existing_access_key = secrets.get("S3_ACCESS_KEY")
        existing_secret_key = secrets.get("S3_SECRET_KEY")

        if bool(existing_access_key) ^ bool(existing_secret_key):
            warning("Partial MinIO credentials found in Vault; generating a new access/secret pair")
            existing_access_key = None
            existing_secret_key = None

        if existing_access_key and existing_secret_key:
            info("MinIO credentials already exist in Vault, skipping bucket creation")
            # The never-expire invariant must hold for pre-existing buckets too.
            cls._ensure_never_expires(c, bucket_name)
            return

        if shutil.which("docker") is None:
            # The iac-runner deploys through the Dokploy API and has no docker
            # CLI/socket, so create_app_bucket (docker exec ... mc) can never
            # work from a deploy — that silent degradation is how the first
            # truealpha staging deploys "succeeded" with no bucket. Point at
            # the host-side path instead of warning vaguely.
            warning(f"docker CLI unavailable — cannot provision bucket '{bucket_name}' from this deploy")
            info(
                "Run ONCE on the VPS host: VAULT_TOKEN=... bash "
                "truealpha/truealpha/10.app/provision_bucket.sh <staging|production>, "
                "then restart the app vault-agent."
            )
            return

        header("MinIO Bucket Setup", f"Creating raw-archive bucket: {bucket_name}")
        # lifecycle_days=0 means create_app_bucket ADDS no expiry rule — the raw
        # archive is the append-only, immutable source-of-record (point-in-time
        # replay reads it forever), unlike report/statement buckets.
        minio_result = create_app_bucket(
            c,
            bucket_name=bucket_name,
            access_key=existing_access_key,
            secret_key=existing_secret_key,
            enable_encryption=True,
            lifecycle_days=0,
            enable_versioning=False,
            public_download=False,
        )
        if not minio_result:
            warning("MinIO bucket creation failed, please configure manually")
            return
        cls._ensure_never_expires(c, bucket_name)

        for key, value in (
            ("S3_ACCESS_KEY", minio_result["access_key"]),
            ("S3_SECRET_KEY", minio_result["secret_key"]),
            ("S3_BUCKET", bucket_name),
        ):
            if not secrets.get(key):
                if secrets.set(key, value):
                    success(f"Vault: {key} stored")
                else:
                    warning(f"Failed to store {key} in Vault")

    @classmethod
    def _ensure_never_expires(cls, c, bucket_name):
        """Remove ALL lifecycle rules from the raw-archive bucket.

        create_app_bucket(lifecycle_days=0) only refrains from ADDING an expiry
        rule — it never removes one, so a bucket that predates this deploy with
        an ILM policy attached would silently keep deleting raw objects. The
        raw archive must never expire; clearing the rules makes that true
        regardless of the bucket's history."""
        if shutil.which("docker") is None:
            info("docker CLI unavailable — lifecycle check runs host-side via provision_bucket.sh")
            return
        env_suffix = get_env().get("ENV_SUFFIX", "")
        container = f"platform-minio{env_suffix}"
        result = c.run(
            f"docker exec {container} mc ilm rm --all --force local/{bucket_name}",
            hide=True,
            warn=True,
        )
        output = f"{result.stdout or ''} {result.stderr or ''}".lower()
        if result.ok:
            success(f"Lifecycle rules cleared on '{bucket_name}' — raw archive never expires")
        elif "does not exist" in output or "no lifecycle" in output:
            info(f"No lifecycle rules on '{bucket_name}' — raw archive never expires")
        else:
            warning(f"Could not clear lifecycle rules on '{bucket_name}', verify manually: {result.stderr}")


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
