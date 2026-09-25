"""Todo service deployment — the Canary Infrastructure Verification Tool."""

from __future__ import annotations

import sys

from invoke import task

from libs.console import error, header, run_with_status, success
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
    secret_key = ""

    backups = ()

    exemptions = (
        Exemption(
            check_id="probes",
            reason="canary verification tool — self-proving via /api/canary/status",
        ),
    )


@task
def sso_setup(c):
    """Register Canary Todo in Authentik SSO (One-time or idempotent)"""
    env = TodoDeployer.env()
    suffix = env.get("ENV_SUFFIX") or ""
    domain_suffix = env.get("ENV_DOMAIN_SUFFIX") or ""
    internal_domain = env.get("INTERNAL_DOMAIN")

    if not internal_domain:
        error("Missing INTERNAL_DOMAIN")
        return

    todo_url = f"https://todo{domain_suffix}.{internal_domain}"
    internal_host = f"platform-todo{suffix}"
    app_name = f"Canary Todo{(' (' + domain_suffix.lstrip('-') + ')') if domain_suffix else ''}"
    app_slug = f"canary-todo{domain_suffix}"

    header("Todo SSO Setup", f"Registering {todo_url} in Authentik")

    res = run_with_status(
        c, "invoke authentik.shared.create-root-token", "Ensure Root Token"
    )
    if not res.ok:
        return

    res = run_with_status(
        c, "invoke authentik.shared.setup-admin-group", "Ensure Admin Group"
    )
    if not res.ok:
        return

    cmd = (
        f"invoke authentik.shared.create-proxy-app "
        f"--name='{app_name}' "
        f"--slug='{app_slug}' "
        f"--external-host='{todo_url}' "
        f"--internal-host='{internal_host}' "
        f"--port=8000"
    )
    res = run_with_status(c, cmd, "Create SSO Application")
    if not res.ok:
        return

    success("Todo SSO setup complete")


if shared_tasks:
    _tasks = make_tasks(TodoDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
