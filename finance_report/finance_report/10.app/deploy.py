import sys

from libs.deployer import Deployer, make_tasks
from libs.console import header, success, info, warning

shared_tasks = sys.modules.get("finance_report.finance_report.10.app.shared")


class AppDeployer(Deployer):
    """Finance Report Application Deployer (Backend + Frontend)."""

    service = "app"
    compose_path = "finance_report/finance_report/10.app/compose.yaml"
    data_path = None
    secret_key = "DATABASE_URL"
    project = "finance_report"

    subdomain = None
    service_port = 3000
    service_name = "frontend"

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

        return env_vars

    @classmethod
    def _ensure_minio_bucket(cls, c):
        """Ensure MinIO bucket exists with proper security configuration."""
        import sys
        from pathlib import Path

        repo_root = Path(__file__).parents[3]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

        from platform.minio.shared import create_app_bucket

        secrets = cls.secrets()
        bucket_name = secrets.get("S3_BUCKET") or "finance-report-statements"
        existing_access_key = secrets.get("S3_ACCESS_KEY")
        existing_secret_key = secrets.get("S3_SECRET_KEY")

        if existing_access_key and existing_secret_key:
            info(f"MinIO credentials already exist in Vault, skipping bucket creation")
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
            public_download=True,
        )

        if not minio_result:
            warning("MinIO bucket creation failed, please configure manually")
            return

        if not existing_access_key:
            if secrets.set("S3_ACCESS_KEY", minio_result["access_key"]):
                success(f"Vault: S3_ACCESS_KEY stored")
            else:
                warning("Failed to store S3_ACCESS_KEY in Vault")

        if not existing_secret_key:
            if secrets.set("S3_SECRET_KEY", minio_result["secret_key"]):
                success(f"Vault: S3_SECRET_KEY stored")
            else:
                warning("Failed to store S3_SECRET_KEY in Vault")

        if not secrets.get("S3_BUCKET"):
            if secrets.set("S3_BUCKET", bucket_name):
                success(f"Vault: S3_BUCKET stored")


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
