from invoke import task
from libs.common import check_service


@task
def status(c):
    return check_service(c, "minio", "mc ready local")
