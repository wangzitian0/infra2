"""Free service shared tasks"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check local free service status"""
    return check_service(c, "free", "sing-box version")
