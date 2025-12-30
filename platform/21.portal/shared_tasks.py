"""Portal shared tasks"""
from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check local Homer portal status (localhost health check)"""
    return check_service(c, "portal", "wget -q --spider http://127.0.0.1:8080/")
