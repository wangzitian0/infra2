"""Todo service deployment — the Canary Infrastructure Verification Tool."""

from __future__ import annotations

import sys

from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import Exemption

shared_tasks = sys.modules.get("platform.30.todo.shared")


class TodoDeployer(Deployer):
    service = "todo"
    compose_path = "platform/30.todo/compose.yaml"
    data_path = ""

    subdomain = None
    service_port = 8000
    service_name = "todo"
    deploy_v2_canary = False

    backups = ()

    exemptions = (
        Exemption(
            check_id="probes",
            reason="canary verification tool — self-proving via /api/canary/status and deploy_v2_canary",
        ),
    )


if shared_tasks:
    _tasks = make_tasks(TodoDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
