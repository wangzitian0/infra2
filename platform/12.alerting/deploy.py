"""Alerting bridge deployment."""

import sys

from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("platform.12.alerting.shared")


class AlertingDeployer(Deployer):
    service = "alerting"
    compose_path = "platform/12.alerting/compose.yaml"
    data_path = "/data/platform/alerting"

    subdomain = None
    service_port = 8080
    service_name = "feishu-alert-bridge"


if shared_tasks:
    _tasks = make_tasks(AlertingDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
