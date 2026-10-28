import datetime
import hashlib
import hmac
import sys
import urllib.error
import urllib.parse
import urllib.request

from common.deploy.deployer import Deployer, make_tasks
from common.console import header, success, info, warning, error
from common.core.facets import (
    ProbeFacet,
    PublicRouteFacet,
    SecretsFacet,
    SignalFacet,
    StorageFacet,
)
from common.observability.openpanel import openpanel_env
from common.security.store import VaultSecrets, get_secrets

shared_tasks = sys.modules.get("finance_report.10.app.shared")


def _s3_bucket_request(
    method: str,
    endpoint_url: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    *,
    region: str = "us-east-1",
    timeout: float = 5.0,
) -> int:
    """Send a SigV4-signed HEAD or PUT request for a bucket and return HTTP status."""
    url = f"{endpoint_url.rstrip('/')}/{bucket}"
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    uri = parsed.path or "/"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = (
        f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (
        f"{method}\n{uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "s3")
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    req = urllib.request.Request(
        url,
        data=b"" if method == "PUT" else None,
        method=method,
        headers={
            "Host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": authorization,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


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
        # #921: `?full=1` asserts every required dependency (finance_report#1653);
        # it answered 200 with all dependencies true in both envs on 2026-09-24.
        # P2 on purpose: it 503s when ANY dependency does, including OpenPanel
        # (whose own probes page it at P1) and Yahoo market data (third party).
        # Its unique catch is the app's own view of a dependency (an expired key,
        # a wrong endpoint), not an outage another probe already pages. Its
        # dependency checks run in sequence, hence the 15 s timeout.
        PublicRouteFacet(
            name="finance-report-api-full-health-public-route",
            subdomain="report",
            path="/api/health?full=1",
            severity="warning",
            timeout_seconds=15,
        ),
    )
    service_port = 3000
    service_name = "frontend"

    # Minute-tier internal probes, the layer the public routes cannot give: an edge
    # failure and an app failure read the same from outside. Both containers sit on
    # dokploy-network and answer from the runner (checked live 2026-09-15); the
    # prefect worker exposes no HTTP and is covered by its own healthcheck.
    probes = (
        ProbeFacet(
            name="finance-report-backend-http",
            kind="http",
            target="http://finance_report-backend${ENV_SUFFIX}:8000/health",
            expected="200",
        ),
        ProbeFacet(
            name="finance-report-frontend-http",
            kind="http",
            target="http://finance_report-frontend${ENV_SUFFIX}:3000/",
            expected="200",
        ),
    )
    signals = (
        SignalFacet(
            tier="minute",
            type="alert",
            consecutive_failures=3,
            renotify_window_sec=0,
        ),
    )
    storage = (
        StorageFacet(
            bucket="statements",
            lifecycle_days=90,
            versioning=False,
            encryption=False,
        ),
    )
    telemetry_service_name = "finance-report-backend"
    telemetry_component = "backend"

    # deploy_v2 acceptance canary support (#541, #1287): app-deploy-request.yml
    # passes --service explicitly for every app's prod preflight canary
    # (including truealpha/app). Declaring deploy_v2_canary = True here marks
    # finance_report/app as the single representative service for ops-checks.yml's
    # default engine canary, avoiding duplicate sequential canary runs on the
    # shared lock.
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
    #      app secrets (staging's, fixed in the generated template), so the derived
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
    def compose_env_overrides(
        cls, *, env: str, domain: str, env_suffix: str
    ) -> dict[str, str]:
        """Environment-specific values the generated secrets.ctmpl no longer defaults.

        Production takes the compose file's inline defaults; staging carried these three
        distinct values in Vault on 2026-09-07 (#633). Bucket names stay in step with
        _ensure_minio_bucket, which reads S3_BUCKET from Vault for the same environment.
        """
        if env == "staging":
            return {
                "S3_BUCKET": "finance-report-staging",
                "S3_PUBLIC_BUCKET": "finance-report-staging",
                "API_RATE_LIMIT_REQUESTS": "2000",
            }
        return {}

    @classmethod
    def compose_env_base(cls, env: dict | None = None) -> dict[str, str]:
        """Include compose_env_overrides in the sync pipeline's compose env (#1279).

        Deployer.composing() builds its Dokploy env payload from compose_env_base and
        replaces the compose env wholesale. Forwarding to compose_env_overrides keeps
        both deploy_v2 (common.deploy.promote) and iac-runner sync aligned on S3_BUCKET,
        S3_PUBLIC_BUCKET, and API_RATE_LIMIT_REQUESTS.
        """
        base = super().compose_env_base(env)
        base.update(
            cls.compose_env_overrides(
                env=base["ENV"],
                domain=base.get("INTERNAL_DOMAIN") or "",
                env_suffix=base.get("ENV_DOMAIN_SUFFIX") or "",
            )
        )
        return base

    @classmethod
    def pre_compose(cls, c) -> dict | None:
        """Prepare environment and ensure MinIO bucket is configured."""
        header(f"{cls.service} pre_compose", "Setting up application dependencies")

        env_vars = super().pre_compose(c)
        if env_vars is None:
            return None

        # Auto-configure S3 Public Endpoint using standardized infrastructure lib
        # This ensures we respect the central SERVICE_SUBDOMAINS definition ("s3")
        # and handle environment suffixes automatically.
        from common.core.environ import get_service_url

        try:
            # "s3" is the canonical key in SERVICE_SUBDOMAINS for the S3 interface
            env_vars["S3_PUBLIC_ENDPOINT"] = get_service_url("s3", env=env_vars)
        except Exception as e:
            from common.console import error

            error(f"Could not resolve Public S3 URL: {e}")
            # Halt deployment when S3 endpoint cannot be resolved to avoid incomplete configuration
            return None

        # Ensure S3 bucket exists with proper security configuration
        cls._ensure_s3_bucket(c)

        # OpenPanel PV tracking (model B: one project per environment). Client ids
        # are PUBLIC web client ids (config, not secret); unknown env => empty =>
        # Analytics no-op. #372: the map is the single source in tools.openpanel_clients
        # so the live deploy_v2 path (common/deploy/promote) injects the same values
        # — this legacy pre_compose path no longer owns a duplicate literal.
        env_vars.update(openpanel_env(str(env_vars.get("ENV") or "")))

        return env_vars

    @classmethod
    def _ensure_s3_bucket(cls, c):
        s3_shared = sys.modules.get("platform.03.s3.shared")
        if not s3_shared:
            raise RuntimeError(
                "S3 shared tasks module (platform.03.s3.shared) not loaded; cannot provision bucket"
            )
        create_app_bucket = getattr(s3_shared, "create_app_bucket", None)
        if not create_app_bucket:
            raise RuntimeError(
                "S3 shared task create_app_bucket not found on platform.03.s3.shared; cannot provision bucket"
            )

        secrets = cls.secrets_backend()
        bucket_name = (
            cls.compose_env_base(cls.env()).get("S3_BUCKET")
            or secrets.get("S3_BUCKET")
            or "statements"
        )  # = compose default (#637)
        existing_access_key = secrets.get("S3_ACCESS_KEY")
        existing_secret_key = secrets.get("S3_SECRET_KEY")

        if bool(existing_access_key) ^ bool(existing_secret_key):
            warning(
                "Partial S3 credentials found in Vault; generating a new access/secret pair"
            )
            existing_access_key = None
            existing_secret_key = None

        if existing_access_key and existing_secret_key:
            info("S3 credentials already exist in Vault, skipping bucket creation")
            info(
                f"To recreate bucket, run: invoke s3.create-app-bucket --bucket-name={bucket_name}"
            )
            return

        header("S3 Bucket Setup", f"Creating application bucket: {bucket_name}")

        minio_result = create_app_bucket(
            c,
            bucket_name=bucket_name,
            access_key=existing_access_key,
            secret_key=existing_secret_key,
            enable_encryption=False,
            lifecycle_days=90,
            enable_versioning=False,
            public_download=False,
        )

        if not minio_result:
            error("S3 bucket creation failed")
            raise RuntimeError(f"Failed to create S3 bucket '{bucket_name}'")

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

    _ensure_minio_bucket = _ensure_s3_bucket

    _s3_bucket_request = staticmethod(_s3_bucket_request)

    @classmethod
    def _ensure_s3_bucket_via_api(
        cls,
        *,
        env: str | None = None,
        endpoint_url: str = "http://platform-s3:9000",
    ) -> bool:
        e = cls._env_for(env)
        effective_env = env or cls._require_env_name(e)
        secrets = cls.secrets_backend(env=effective_env)
        try:
            access_key = secrets.get("S3_ACCESS_KEY")
            secret_key = secrets.get("S3_SECRET_KEY")
            stored_bucket = secrets.get("S3_BUCKET")
        except VaultSecrets.VaultSecretNotFoundError:
            return True
        if not (access_key and secret_key):
            return True
        bucket_name = (
            cls.compose_env_overrides(
                env=effective_env,
                domain=e.get("INTERNAL_DOMAIN") or "",
                env_suffix=e.get("ENV_DOMAIN_SUFFIX") or "",
            ).get("S3_BUCKET")
            or stored_bucket
            or "statements"
        )
        try:
            status = cls._s3_bucket_request(
                "HEAD", endpoint_url, bucket_name, access_key, secret_key
            )
        except urllib.error.URLError as exc:
            warning(
                f"S3 endpoint {endpoint_url} not reachable from this context ({exc}); "
                "skipping bucket API check"
            )
            return True
        if status == 200:
            info(f"S3 bucket '{bucket_name}' verified reachable via S3 API")
            return True
        if status == 404:
            warning(
                f"S3 bucket '{bucket_name}' returned 404 on {endpoint_url}; "
                "creating with S3 root credentials"
            )
            try:
                root_secrets = get_secrets("platform", "minio", "production")
                root_user = root_secrets.get("root_user") or "admin"
                root_password = root_secrets.get("root_password")
            except VaultSecrets.VaultError as exc:
                error(f"Could not read S3 root credentials from Vault: {exc}")
                return False
            if not root_password:
                error(
                    "S3 root_password missing in secret/platform/production/minio; "
                    f"cannot create bucket '{bucket_name}'"
                )
                return False
            put_status = cls._s3_bucket_request(
                "PUT", endpoint_url, bucket_name, root_user, root_password
            )
            if put_status not in (200, 204, 409):
                error(
                    f"Failed to create S3 bucket '{bucket_name}' on {endpoint_url} "
                    f"(HTTP {put_status})"
                )
                return False
            verify_status = cls._s3_bucket_request(
                "HEAD", endpoint_url, bucket_name, access_key, secret_key
            )
            if verify_status == 200:
                success(
                    f"S3 bucket '{bucket_name}' created and verified on {endpoint_url}"
                )
                return True
            error(
                f"S3 bucket '{bucket_name}' verification failed after creation "
                f"(HTTP {verify_status})"
            )
            return False
        error(
            f"S3 bucket '{bucket_name}' check failed on {endpoint_url} (HTTP {status})"
        )
        return False

    @classmethod
    def apply_secret_supply(cls, c, *, env: str | None = None) -> bool:
        if not super().apply_secret_supply(c, env=env):
            return False
        return cls._ensure_s3_bucket_via_api(env=env)


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
