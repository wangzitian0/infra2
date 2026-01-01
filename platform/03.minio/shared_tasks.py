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
        from libs.console import info, success, warning
        endpoints = [
            (f"https://minio.{domain}", "Console"),
            (f"https://s3.{domain}", "S3 API"),
        ]
        info(f"Checking external endpoints for domain: {domain}")
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

