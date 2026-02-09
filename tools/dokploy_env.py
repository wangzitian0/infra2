"""Dokploy environment management CLI tasks."""
from __future__ import annotations

from invoke import task
from rich.table import Table

from libs.common import normalize_env_name, get_env, get_service_url
from libs.console import header, success, error, info, console, warning
from libs.dokploy import get_dokploy


@task
def env_list(c, project: str = "platform", host: str | None = None):
    """List environments for a Dokploy project."""
    header("Dokploy Environments", f"Project: {project}")
    client = get_dokploy(host=host)
    envs = client.list_environments(project)
    if not envs:
        error(f"No environments found for project '{project}'")
        return

    table = Table(show_header=True)
    table.add_column("Name")
    table.add_column("Default")
    table.add_column("Environment ID")
    for env in envs:
        table.add_row(
            env.get("name") or "",
            "yes" if env.get("isDefault") else "",
            env.get("environmentId") or "",
        )
    console.print(table)


@task
def env_ensure(c, project: str = "platform", env: str = "staging", description: str = "", host: str | None = None):
    """Ensure a Dokploy environment exists for a project."""
    env_name = normalize_env_name(env)
    header("Dokploy Environment", f"{project}/{env_name}")
    client = get_dokploy(host=host)
    env_obj, created = client.ensure_environment(project, env_name, description=description)
    if created:
        success(f"Created environment '{env_name}'")
    else:
        info(f"Environment '{env_name}' already exists")
    console.print({"environmentId": env_obj.get("environmentId"), "name": env_obj.get("name")})


@task
def logs(c, name, project: str = "platform", env: str | None = None, deployment: bool = False, tail: int = 50):
    """Show logs for a Dokploy compose application.
    
    If --deployment is set, shows deployment (build) logs.
    Otherwise, shows container runtime logs.
    """
    header("Dokploy Logs", f"{project}/{env or 'default'}/{name}")
    client = get_dokploy()
    e = get_env()
    
    compose = client.find_compose_by_name(name, project_name=project, env_name=env)
    if not compose:
        error(f"Compose application '{name}' not found")
        return

    compose_id = compose["composeId"]
    host = e.get("VPS_HOST")
    if not host:
        error("VPS_HOST not configured in 1Password (init/env_vars) or environment")
        return

    # Check for SigNoz/OTel configuration
    app_env = client.get_compose_env(compose_id)
    if "OTEL_EXPORTER_OTLP_ENDPOINT" in app_env or "SIGNOZ_OTLP_ENDPOINT" in app_env:
        try:
            signoz_url = get_service_url("signoz", env=e)
            warning("This application appears to use SigNoz for logging.")
            info(f"SigNoz Dashboard: {signoz_url}/logs")
        except Exception:
            warning("This application appears to use SigNoz for logs, but SigNoz URL could not be determined.")
    
    if deployment:
        # Show build logs
        latest = client.get_latest_deployment(compose_id)
        if not latest:
            error("No deployments found")
            return
        
        log_path = latest.get("logPath")
        if not log_path:
            error("No log path found in deployment metadata")
            return
        
        info(f"Fetching deployment logs from {log_path}...")
        c.run(f"ssh root@{host} 'tail -n {tail} {log_path}'")
    else:
        # Show container logs
        details = client.get_compose(compose_id)
        app_name = details.get("appName") 
        
        info(f"Fetching container logs for {app_name}*...")
        # Get all containers belonging to this compose (based on appName prefix)
        cmd = f"ssh root@{host} \"docker ps -a --filter name={app_name} --format '{{{{.Names}}}}' | xargs -I % sh -c 'echo [bold cyan]--- % ---[/]; docker logs --tail {tail} %'\""
        c.run(cmd)
