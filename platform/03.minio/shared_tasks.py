from invoke import task
from libs.common import check_service, get_env


@task
def status(c):
    """Check MinIO status: container health + both endpoints."""
    # Check container health via docker
    result = check_service(c, "minio", "mc ready local")
    
    if not result.get("is_ready"):
        return result
    
    # Also verify external endpoints are reachable
    e = get_env()
    domain = e.get("INTERNAL_DOMAIN")
    if domain:
        endpoints = [
            (f"https://s3.{domain}", "Console"),
            (f"https://minio.{domain}", "S3 API"),
        ]
        for url, name in endpoints:
            check = c.run(f"curl -sI {url} -o /dev/null -w '%{{http_code}}'", hide=True, warn=True)
            code = check.stdout.strip() if check.ok else "error"
            if code not in ("200", "403"):  # 403 is OK for S3 API without auth
                result["details"] += f"; {name}: HTTP {code}"
    
    return result

