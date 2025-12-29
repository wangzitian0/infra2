"""Portal shared tasks"""
from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check Homer portal status"""
    return check_service(c, "portal", "wget -q --spider http://127.0.0.1:8080/")
