import sys

from libs.deploy.deployer import Deployer, make_tasks
from libs.console import header, success, info, warning
from libs.service_facets import PublicRouteFacet, SecretsFacet
from tools.openpanel_clients import openpanel_env

shared_tasks = sys.modules.get("finance_report.10.app.shared")


class AppDeployer(Deployer):
    """Finance Report Application Deployer (Backend + Frontend)."""

    service = "app"
    compose_path = "finance_report/finance_report/10.app/compose.yaml"
    data_path = None
    secret_key = "DATABASE_URL"
    project = "finance_report"

    subdomain = None

    # Public routes probed from inside (#543, #209 reversed): TWO registered
    # signals for this one service — the web root and the API health surface —
    # matching the Cloudflare watchdog's own split. subdomain override because
    # this bespoke app's Deployer declares none (routing lives in the compose).
    public_routes = (
        PublicRouteFacet(
            name="finance-report-web-public-route",
            subdomain="report",
        ),
        PublicRouteFacet(
            name="finance-report-api-public-route",
            subdomain="report",
            path="/api/health",
        ),
    )
    service_port = 3000
    service_name = "frontend"
    telemetry_service_name = "finance-report-backend"
    telemetry_component = "backend"

    # deploy_v2 acceptance canary support (#541): tools/deploy_v2_canary.py
    # iterates registry services declaring this flag (was a hardcoded service
    # id there). Requires a working preview lane (the canary deploys the
    # reserved pr-<_CANARY_PR> slot); truealpha flips its own flag when its
    # preview lands — no canary code change needed then.
    deploy_v2_canary = True

    # Vault self-refresh facts (#542): the audit inventory derives from these
    # (AppRole auth — the #257/#259 pilot). Two surfaces:
    #   1. the app itself. optional_inert_fields (#526): LLM_ENCRYPTION_KEYS'
    #      secrets.ctmpl render line landed in #482/PR#520; the Vault value is
    #      intentionally unset until EPIC-023's DB-backed LLM-provider-secret
    #      storage is turned on — reported informationally, never a failure.
    #   2. the multi-alias ephemeral PREVIEW surface (declared here with an
    #      explicit service_id — an alias stack has no registry Deployer of its
    #      own). Same AppRole auth; its secrets template reads the SOURCE env's
    #      app secrets (PREVIEW_SECRET_ENV, default staging), so the derived
    #      vault path {env} resolves to that source env, not the alias.
    secrets = (
        SecretsFacet(
            vault_agent_container="finance_report-app-vault-agent${ENV_SUFFIX}",
            app_containers=("finance_report-backend${ENV_SUFFIX}",),
            auth_method="approle",
            optional_inert_fields=("LLM_ENCRYPTION_KEYS",),
        ),
        SecretsFacet(
            service_id="finance_report/preview",
            compose_path="finance_report/finance_report/preview/compose.yaml",
            vault_agent_container="finance_report-app-vault-agent${ENV_SUFFIX}",
            app_containers=("finance_report-backend${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )

    @classmethod
    def pre_compose(cls, c) -> dict | None:
        """Prepare environment and ensure MinIO bucket is configured."""
        header(f"{cls.service} pre_compose", "Setting up application dependencies")

        env_vars = super().pre_compose(c)
        if env_vars is None:
            return None

        # Auto-configure S3 Public Endpoint using standardized infrastructure lib
        # This ensures we respect the central SERVICE_SUBDOMAINS definition (minio_api -> s3)
        # and handle environment suffixes automatically.
        from libs.common import get_service_url

        try:
            # "minio_api" is the key in SERVICE_SUBDOMAINS for the S3 interface
            env_vars["S3_PUBLIC_ENDPOINT"] = get_service_url("minio_api", env=env_vars)
        except Exception as e:
            from libs.console import error

            error(f"Could not resolve Public S3 URL: {e}")
            # Halt deployment when S3 endpoint cannot be resolved to avoid incomplete configuration
            return None

        # Ensure MinIO bucket exists with proper security configuration
        cls._ensure_minio_bucket(c)

        # OpenPanel PV tracking (model B: one project per environment). Client ids
        # are PUBLIC web client ids (config, not secret); unknown env => empty =>
        # Analytics no-op. #372: the map is the single source in tools.openpanel_clients
        # so the live deploy_v2 path (libs/deploy/promote) injects the same values
        # — this legacy pre_compose path no longer owns a duplicate literal.
        env_vars.update(openpanel_env(env_vars.get("ENV", "production")))

        return env_vars

    @classmethod
    def _ensure_minio_bucket(cls, c):
        """Ensure MinIO bucket exists with proper security configuration."""
        minio_shared = sys.modules.get("platform.03.minio.shared")
        if not minio_shared:
            warning("MinIO shared tasks module not loaded; skipping bucket creation")
            return
        create_app_bucket = getattr(minio_shared, "create_app_bucket", None)
        if not create_app_bucket:
            warning(
                "MinIO shared task create_app_bucket not found; skipping bucket creation"
            )
            return

        secrets = cls.secrets_backend()
        bucket_name = secrets.get("S3_BUCKET") or "finance-report-statements"
        existing_access_key = secrets.get("S3_ACCESS_KEY")
        existing_secret_key = secrets.get("S3_SECRET_KEY")

        if bool(existing_access_key) ^ bool(existing_secret_key):
            warning(
                "Partial MinIO credentials found in Vault; generating a new access/secret pair"
            )
            existing_access_key = None
            existing_secret_key = None

        if existing_access_key and existing_secret_key:
            info("MinIO credentials already exist in Vault, skipping bucket creation")
            info(
                f"To recreate bucket, run: invoke minio.create-app-bucket --bucket-name={bucket_name}"
            )
            return

        header("MinIO Bucket Setup", f"Creating application bucket: {bucket_name}")

        minio_result = create_app_bucket(
            c,
            bucket_name=bucket_name,
            access_key=existing_access_key,
            secret_key=existing_secret_key,
            enable_encryption=True,
            lifecycle_days=90,
            enable_versioning=False,
            public_download=False,
        )

        if not minio_result:
            warning("MinIO bucket creation failed, please configure manually")
            return

        if not existing_access_key:
            if secrets.set("S3_ACCESS_KEY", minio_result["access_key"]):
                success("Vault: S3_ACCESS_KEY stored")
            else:
                warning("Failed to store S3_ACCESS_KEY in Vault")

        if not existing_secret_key:
            if secrets.set("S3_SECRET_KEY", minio_result["secret_key"]):
                success("Vault: S3_SECRET_KEY stored")
            else:
                warning("Failed to store S3_SECRET_KEY in Vault")

        if not secrets.get("S3_BUCKET"):
            if secrets.set("S3_BUCKET", bucket_name):
                success("Vault: S3_BUCKET stored")


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
