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
CODE_SERVER_HEALTH_COMMAND = (
    "dagster api grpc-health-check --socket /var/lib/dagster/code-server.sock"
)


@task
def status(c):
    """Check the loopback-only Dagster UI, persistent daemon heartbeat, and code server."""
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
    code_server = check_service(
        c,
        "truealpha-dagster-code-server",
        CODE_SERVER_HEALTH_COMMAND,
    )
    return {
        "is_ready": web["is_ready"] and daemon["is_ready"] and code_server["is_ready"],
        "details": (
            f"web={web['details']}, daemon={daemon['details']}, "
            f"code_server={code_server['details']}"
        ),
    }
