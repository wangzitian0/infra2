"""Facts of the app data-store presets (#1825 Slice 1).

An App's PostgreSQL or Redis stack subclasses ``PostgresStackDeployer`` or
``RedisStackDeployer`` (``common/deploy/stack_presets.py``) and sets only
``project``. This module is the one definition of the facts that the preset
derives from ``project``.

Two readers use it:

- ``common/deploy/stack_presets.py`` sets the facts as class attributes at import.
- ``common/core/registry.py`` and ``common/backup/verification.py`` read deploy.py
  through AST and never import it. ``preset_defaults`` gives them the same facts.

A literal attribute in the subclass body always wins over a preset fact.
This module imports only the facet types, so the AST readers stay import-free.
"""

from __future__ import annotations

import ast
from typing import Any

from common.core.facets import BackupFacet, ProbeFacet, SecretsFacet, SignalFacet

#: Static facts per preset kind. ``{project}`` is the Dokploy project name.
_KINDS: dict[str, dict[str, Any]] = {
    "postgres": {
        "dir": "01.postgres",
        "port": 5432,
        "secret_key": "POSTGRES_PASSWORD",
        # PostgreSQL Alpine requires uid=70 and chmod=700.
        "uid": "70",
        "chmod": "700",
        "backup_method": "pg_dump_plus_data_archive",
        "restore_command": "restore latest {project} pg_dump, then reattach DATA_PATH if needed.",
    },
    "redis": {
        "dir": "02.redis",
        "port": 6379,
        "secret_key": "PASSWORD",
        "uid": "999",
        "chmod": "755",
        "backup_method": "redis_rdb_archive",
        "restore_command": "stop Redis, restore dump.rdb, then start Redis.",
    },
}

#: The preset base class name -> preset kind.
PRESET_BASES: dict[str, str] = {
    "PostgresStackDeployer": "postgres",
    "RedisStackDeployer": "redis",
}

#: Authenticated, like the compose healthcheck: the server runs --requirepass, and a
#: bare `redis-cli ping` answers NOAUTH with exit 0, which check_service reads as
#: ready (#713). check_service runs this under `sh -lc` inside the container.
REDIS_HEALTH_COMMAND = '. /secrets/.env && redis-cli -a "$PASSWORD" ping'


def health_command(kind: str, project: str) -> str:
    """The `status` task command; it matches the compose healthcheck of the stack."""
    if kind == "postgres":
        return f"pg_isready -U postgres -d {project}"
    return REDIS_HEALTH_COMMAND


def preset_attrs(kind: str, project: str) -> dict[str, Any]:
    """The Deployer class attributes that the preset ``kind`` derives from ``project``."""
    facts = _KINDS[kind]
    port = facts["port"]
    container = f"{project}-{kind}"
    return {
        "service": kind,
        "service_name": kind,
        "compose_path": f"{project}/{project}/{facts['dir']}/compose.yaml",
        "data_path": f"/data/{project}/{kind}",
        "secret_key": facts["secret_key"],
        "uid": facts["uid"],
        "chmod": facts["chmod"],
        # No public domain: the stack is internal only.
        "subdomain": None,
        "service_port": port,
        "backups": (
            BackupFacet(
                method=facts["backup_method"],
                restore_command=facts["restore_command"].format(project=project),
            ),
        ),
        # Minute-tier liveness from the probe runner. TCP only: the credentials of
        # the app are not the credentials of the runner, so a protocol probe would
        # only prove the password.
        "probes": (
            ProbeFacet(
                name=f"{project.replace('_', '-')}-{kind}-tcp",
                kind="tcp",
                target=f"{container}${{ENV_SUFFIX}}:{port}",
                expected="connected",
            ),
        ),
        "signals": (
            SignalFacet(
                tier="minute",
                type="alert",
                consecutive_failures=3,
                renotify_window_sec=0,
            ),
        ),
        # Vault self-refresh facts (#542), AppRole auth (#257/#259).
        "secrets": (
            SecretsFacet(
                vault_agent_container=f"{container}-vault-agent${{ENV_SUFFIX}}",
                app_containers=(f"{container}${{ENV_SUFFIX}}",),
                auth_method="approle",
            ),
        ),
    }


def preset_defaults(tree: ast.Module) -> dict[str, Any]:
    """Preset facts for the top-level preset Deployer class of a deploy.py AST.

    Returns ``{}`` when no top-level class subclasses a preset. A preset class
    without a literal ``project`` string raises ``ValueError``: the AST readers fail
    closed, so a service never loses its facts without a CI failure.
    """
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        kind = next(
            (
                PRESET_BASES[base.id]
                for base in node.bases
                if isinstance(base, ast.Name) and base.id in PRESET_BASES
            ),
            None,
        )
        if kind is None:
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "project" for t in stmt.targets
                )
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                return preset_attrs(kind, stmt.value.value)
        raise ValueError(
            f"class {node.name}: a {kind} stack preset needs a literal `project` string"
        )
    return {}
