"""Dokploy environment management CLI tasks."""

from __future__ import annotations

from invoke import task
from rich.table import Table

from libs.common import normalize_env_name
from libs.console import header, success, error, info, console
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
def env_ensure(
    c,
    project: str = "platform",
    env: str = "staging",
    description: str = "",
    host: str | None = None,
):
    """Ensure a Dokploy environment exists for a project."""
    env_name = normalize_env_name(env)
    header("Dokploy Environment", f"{project}/{env_name}")
    client = get_dokploy(host=host)
    env_obj, created = client.ensure_environment(
        project, env_name, description=description
    )
    if created:
        success(f"Created environment '{env_name}'")
    else:
        info(f"Environment '{env_name}' already exists")
    console.print(
        {"environmentId": env_obj.get("environmentId"), "name": env_obj.get("name")}
    )
