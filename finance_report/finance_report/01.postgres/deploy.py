import sys

from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("finance_report.01.postgres.shared")


class PostgresDeployer(Deployer):
    """Finance Report PostgreSQL Deployer."""

    service = "postgres"
    compose_path = "finance_report/finance_report/01.postgres/compose.yaml"
    data_path = "/data/finance_report/postgres"
    secret_key = "POSTGRES_PASSWORD"
    project = "finance_report"  # Dokploy project name

    # PostgreSQL Alpine requires uid=70 and chmod=700
    uid = "70"
    chmod = "700"

    # No public domain (internal only)
    subdomain = None
    service_port = 5432
    service_name = "postgres"


if shared_tasks:
    _tasks = make_tasks(PostgresDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
