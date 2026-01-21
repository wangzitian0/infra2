from invoke import task
from libs.common import check_service, get_env, service_domain
from libs.env import generate_password
from libs.console import header, success, error, warning, info


@task
def status(c):
    """Check MinIO status: container health + both endpoints."""
    # Check container health via docker
    result = check_service(c, "minio", "mc ready local")

    if not result.get("is_ready"):
        return result

    # Also verify external endpoints are reachable
    e = get_env()
    console_host = service_domain("minio", e)
    api_host = service_domain("s3", e)
    if console_host and api_host:
        endpoints = [
            (f"https://{console_host}", "Console"),
            (f"https://{api_host}", "S3 API"),
        ]
        info(f"Checking external endpoints for domain: {e.get('INTERNAL_DOMAIN')}")
        for url, name in endpoints:
            check = c.run(
                f"curl -sI {url} -o /dev/null -w '%{{http_code}}'", hide=True, warn=True
            )
            code = check.stdout.strip() if check.ok else "error"

            if code == "200":
                success(f"   {name} ({url}): HTTP {code} (OK)")
            elif (
                code == "403" or code == "400"
            ):  # 400/403 is OK for S3 API without auth
                success(f"   {name} ({url}): HTTP {code} (API Active)")
            else:
                warning(f"   {name} ({url}): HTTP {code}")
                result["details"] += f"; {name}: HTTP {code}"

    return result


@task
def create_app_bucket(
    c,
    bucket_name,
    access_key=None,
    secret_key=None,
    enable_encryption=True,
    lifecycle_days=90,
    enable_versioning=False,
    public_download=True,
):
    """Create MinIO bucket with security best practices for application usage.

    Args:
        bucket_name: Name of the bucket to create (e.g., 'finance-report-statements')
        access_key: MinIO access key for the application user (auto-generated if None)
        secret_key: MinIO secret key for the application user (auto-generated if None)
        enable_encryption: Enable server-side encryption (SSE-S3) - default True
        lifecycle_days: Auto-delete files after N days (default 90, 0 to disable)
        enable_versioning: Enable bucket versioning - default False
        public_download: Allow anonymous public download via direct object URLs - default True

    Returns:
        dict: {"access_key": str, "secret_key": str, "bucket": str}

    Example:
        # In downstream deploy.py pre_compose:
        import sys
        minio_shared = sys.modules.get("platform.03.minio.shared")
        create_app_bucket = minio_shared.create_app_bucket

        result = create_app_bucket(
            c,
            bucket_name="finance-report-statements",
            lifecycle_days=90,
        )
        # Store result["access_key"] and result["secret_key"] in Vault
    """
    header("MinIO Bucket Setup", f"Creating bucket: {bucket_name}")

    e = get_env()
    env_suffix = e.get("ENV_SUFFIX", "")
    container_name = f"platform-minio{env_suffix}"

    # Generate credentials if not provided
    if not access_key:
        access_key = bucket_name.replace("-", "_")  # e.g., finance_report_statements
        info(f"Generated access_key: {access_key}")

    if not secret_key:
        secret_key = generate_password(32)
        info("Generated secret_key: <hidden>")

    # Step 1: Create bucket
    info(f"Creating bucket '{bucket_name}'...")
    result = c.run(
        f"docker exec {container_name} mc mb local/{bucket_name} --ignore-existing",
        hide=True,
        warn=True,
    )
    if result.ok:
        success(f"Bucket '{bucket_name}' ready")
    else:
        error(f"Failed to create bucket: {result.stderr}")
        return None

    # Step 2: Set public download policy (enables anonymous object downloads)
    if public_download:
        info(
            "Setting bucket policy: public anonymous download (direct object URL access)..."
        )
        result = c.run(
            f"docker exec {container_name} mc anonymous set download local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success("Public anonymous download enabled (direct object URL access)")
        else:
            warning(f"Failed to set public policy: {result.stderr}")

    # Step 3: Enable server-side encryption
    if enable_encryption:
        info("Enabling server-side encryption (SSE-S3)...")
        result = c.run(
            f"docker exec {container_name} mc encrypt set sse-s3 local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success("Server-side encryption enabled (SSE-S3)")
        else:
            warning(f"Failed to enable encryption: {result.stderr}")

    # Step 4: Set lifecycle policy (auto-delete old files)
    if lifecycle_days > 0:
        info(f"Setting lifecycle policy: auto-delete after {lifecycle_days} days...")
        result = c.run(
            f"docker exec {container_name} mc ilm add local/{bucket_name} "
            f"--expiry-days {lifecycle_days}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success(f"Lifecycle policy: files deleted after {lifecycle_days} days")
        else:
            warning(f"Failed to set lifecycle: {result.stderr}")

    # Step 5: Enable versioning (optional)
    if enable_versioning:
        info("Enabling bucket versioning...")
        result = c.run(
            f"docker exec {container_name} mc version enable local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success("Bucket versioning enabled")
        else:
            warning(f"Failed to enable versioning: {result.stderr}")

    # Step 6: Create access key (service account)
    info(f"Creating MinIO service account: {access_key}...")
    result = c.run(
        f"docker exec {container_name} mc admin user add local {access_key} {secret_key}",
        hide=True,
        warn=True,
    )
    if result.ok:
        success(f"Service account created: {access_key}")
    else:
        # User might already exist, try to update secret
        warning("User may already exist, attempting to update...")
        result = c.run(
            f"docker exec {container_name} mc admin user remove local {access_key}",
            hide=True,
            warn=True,
        )
        result = c.run(
            f"docker exec {container_name} mc admin user add local {access_key} {secret_key}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success(f"Service account updated: {access_key}")
        else:
            error(f"Failed to create/update user: {result.stderr}")
            return None

    # Step 7: Attach readwrite policy to user
    info(f"Attaching readwrite policy to {access_key}...")
    result = c.run(
        f"docker exec {container_name} mc admin policy attach local readwrite "
        f"--user {access_key}",
        hide=True,
        warn=True,
    )
    if result.ok:
        success(f"Policy attached: {access_key} -> readwrite")
    else:
        warning(f"Failed to attach policy: {result.stderr}")

    # Verification
    header("Verification", f"Bucket '{bucket_name}' configuration")
    verification_ok = True

    # Check bucket exists
    result = c.run(
        f"docker exec {container_name} mc ls local/{bucket_name}", hide=True, warn=True
    )
    if not result.ok:
        warning(f"Bucket verification failed: unable to list '{bucket_name}'.")
        verification_ok = False

    # Check policy
    result = c.run(
        f"docker exec {container_name} mc anonymous get local/{bucket_name}",
        hide=True,
        warn=True,
    )
    if not result.ok:
        warning(
            f"Bucket verification failed: unable to read anonymous policy for '{bucket_name}'."
        )
        verification_ok = False

    # Check encryption
    if enable_encryption:
        result = c.run(
            f"docker exec {container_name} mc encrypt info local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if not result.ok:
            warning(
                f"Bucket verification failed: unable to read encryption info for '{bucket_name}'."
            )
            verification_ok = False

    # Check lifecycle
    if lifecycle_days > 0:
        result = c.run(
            f"docker exec {container_name} mc ilm ls local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if not result.ok:
            warning(
                f"Bucket verification failed: unable to read lifecycle rules for '{bucket_name}'."
            )
            verification_ok = False

    if verification_ok:
        success("Bucket setup complete!")
    else:
        warning("Bucket setup completed with verification warnings")
    info("Next steps:")
    info("  1. Store credentials in Vault (project/env/service secrets)")
    info(f"     - S3_ACCESS_KEY={access_key}")
    info("     - S3_SECRET_KEY=<hidden>")
    info(f"     - S3_BUCKET={bucket_name}")

    return {
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket_name,
    }
