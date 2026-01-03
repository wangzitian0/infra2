from invoke import task
from libs.common import check_service, get_env, service_domain


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
        from libs.console import info, success, warning
        endpoints = [
            (f"https://{console_host}", "Console"),
            (f"https://{api_host}", "S3 API"),
        ]
        info(f"Checking external endpoints for domain: {e.get('INTERNAL_DOMAIN')}")
        for url, name in endpoints:
            check = c.run(f"curl -sI {url} -o /dev/null -w '%{{http_code}}'", hide=True, warn=True)
            code = check.stdout.strip() if check.ok else "error"
            
            if code == "200":
                success(f"   {name} ({url}): HTTP {code} (OK)")
            elif code == "403" or code == "400": # 400/403 is OK for S3 API without auth
                success(f"   {name} ({url}): HTTP {code} (API Active)")
            else:
                warning(f"   {name} ({url}): HTTP {code}")
                result["details"] += f"; {name}: HTTP {code}"
    
    return result
