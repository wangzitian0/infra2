from invoke import task
from libs.common import check_service


WEBSERVER_HEALTH_COMMAND = (
    'python -c "import os,urllib.request; '
    'urllib.request.urlopen(\\"http://127.0.0.1:\\" + '
    'os.environ[\\"DAGSTER_WEBSERVER_PORT\\"] + \\"/server_info\\", timeout=3)"'
)
DAEMON_HEALTH_COMMAND = (
    'sh -c ". /secrets/.env; export '
    'DAGSTER_POSTGRES_URL=\\"\\$DATABASE_URL?options=-csearch_path%3Ddagster\\"; '
    'dagster-daemon liveness-check"'
)


@task
def status(c):
    """Check the loopback-only Dagster UI and persistent daemon heartbeat."""
    web = check_service(
        c,
        "truealpha-dagster-webserver",
        WEBSERVER_HEALTH_COMMAND,
    )
    daemon = check_service(
        c,
        "truealpha-dagster-daemon",
        DAEMON_HEALTH_COMMAND,
    )
    return {
        "is_ready": web["is_ready"] and daemon["is_ready"],
        "details": f"web={web['details']}, daemon={daemon['details']}",
    }
