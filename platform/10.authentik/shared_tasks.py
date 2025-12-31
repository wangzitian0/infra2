"""Authentik shared tasks"""
from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check Authentik status"""
    return check_service(c, "authentik", "ak healthcheck")
