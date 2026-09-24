"""Todo canary service shared tasks"""

from invoke import task
from libs.common import check_service


@task
def status(c):
    """Check local todo canary status"""
    return check_service(c, "todo", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')\"")
