"""The App PostgreSQL and Redis stack presets (#1825 Slice 1).

Each App data-store deploy.py sets only `project` on a preset subclass. These tests
pin the facts that the deploy.py files declared literally before the preset, so a
preset edit that changes a production fact fails here. They also prove that the
AST readers and the imported class read the same facts.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from invoke import Collection, Context, Task

import common.common as common_module
from common.backup.verification import discover_deployer_data_paths
from common.core import registry as reg
from common.core.facets import BackupFacet, ProbeFacet, SecretsFacet, SignalFacet
from common.core.stack_preset_facts import preset_defaults
from common.deploy import stack_presets

ROOT = Path(__file__).resolve().parents[2]

_SIGNALS = (
    SignalFacet(
        tier="minute", type="alert", consecutive_failures=3, renotify_window_sec=0
    ),
)
_PG_RESTORE = "restore latest {} pg_dump, then reattach DATA_PATH if needed."

#: The literal facts of each deploy.py at origin/main 721c04e6, before the preset.
PINNED = {
    "finance_report/postgres": {
        "dir": "finance_report/finance_report/01.postgres",
        "class": "PostgresDeployer",
        "service": "postgres",
        "service_name": "postgres",
        "project": "finance_report",
        "compose_path": "finance_report/finance_report/01.postgres/compose.yaml",
        "data_path": "/data/finance_report/postgres",
        "service_port": 5432,
        "subdomain": None,
        "secret_key": "POSTGRES_PASSWORD",
        "uid": "70",
        "chmod": "700",
        "backups": (
            BackupFacet(
                method="pg_dump_plus_data_archive",
                restore_command=_PG_RESTORE.format("finance_report"),
            ),
        ),
        "probes": (
            ProbeFacet(
                name="finance-report-postgres-tcp",
                kind="tcp",
                target="finance_report-postgres${ENV_SUFFIX}:5432",
                expected="connected",
            ),
        ),
        "signals": _SIGNALS,
        "secrets": (
            SecretsFacet(
                vault_agent_container="finance_report-postgres-vault-agent${ENV_SUFFIX}",
                app_containers=("finance_report-postgres${ENV_SUFFIX}",),
                auth_method="approle",
            ),
        ),
        "status": (
            "finance_report-postgres",
            "pg_isready -U postgres -d finance_report",
        ),
    },
    "finance_report/redis": {
        "dir": "finance_report/finance_report/02.redis",
        "class": "RedisDeployer",
        "service": "redis",
        "service_name": "redis",
        "project": "finance_report",
        "compose_path": "finance_report/finance_report/02.redis/compose.yaml",
        "data_path": "/data/finance_report/redis",
        "service_port": 6379,
        "subdomain": None,
        "secret_key": "PASSWORD",
        "uid": "999",
        "chmod": "755",
        "backups": (
            BackupFacet(
                method="redis_rdb_archive",
                restore_command="stop Redis, restore dump.rdb, then start Redis.",
            ),
        ),
        "probes": (
            ProbeFacet(
                name="finance-report-redis-tcp",
                kind="tcp",
                target="finance_report-redis${ENV_SUFFIX}:6379",
                expected="connected",
            ),
        ),
        "signals": _SIGNALS,
        "secrets": (
            SecretsFacet(
                vault_agent_container="finance_report-redis-vault-agent${ENV_SUFFIX}",
                app_containers=("finance_report-redis${ENV_SUFFIX}",),
                auth_method="approle",
            ),
        ),
        "status": (
            "finance_report-redis",
            '. /secrets/.env && redis-cli -a "$PASSWORD" ping',
        ),
    },
    "truealpha/postgres": {
        "dir": "truealpha/truealpha/01.postgres",
        "class": "PostgresDeployer",
        "service": "postgres",
        "service_name": "postgres",
        "project": "truealpha",
        "compose_path": "truealpha/truealpha/01.postgres/compose.yaml",
        "data_path": "/data/truealpha/postgres",
        "service_port": 5432,
        "subdomain": None,
        "secret_key": "POSTGRES_PASSWORD",
        "uid": "70",
        "chmod": "700",
        "backups": (
            BackupFacet(
                method="pg_dump_plus_data_archive",
                restore_command=_PG_RESTORE.format("truealpha"),
            ),
        ),
        "probes": (
            ProbeFacet(
                name="truealpha-postgres-tcp",
                kind="tcp",
                target="truealpha-postgres${ENV_SUFFIX}:5432",
                expected="connected",
            ),
        ),
        "signals": _SIGNALS,
        "secrets": (
            SecretsFacet(
                vault_agent_container="truealpha-postgres-vault-agent${ENV_SUFFIX}",
                app_containers=("truealpha-postgres${ENV_SUFFIX}",),
                auth_method="approle",
            ),
        ),
        "status": ("truealpha-postgres", "pg_isready -U postgres -d truealpha"),
    },
}
_REGISTRY_FIELDS = (
    "service",
    "service_name",
    "project",
    "compose_path",
    "data_path",
    "service_port",
    "subdomain",
    "backups",
    "probes",
    "signals",
    "secrets",
)
_CLASS_FIELDS = _REGISTRY_FIELDS + ("secret_key", "uid", "chmod")


def _load_deploy(service_dir: str, module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / service_dir / "deploy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


@pytest.mark.parametrize("service_id", sorted(PINNED))
def test_registry_reads_the_pinned_facts_through_ast(service_id):
    meta = reg.service_attrs()[service_id]
    pinned = PINNED[service_id]
    for field in _REGISTRY_FIELDS:
        assert getattr(meta, field) == pinned[field], f"{service_id}.{field}"


@pytest.mark.parametrize("service_id", sorted(PINNED))
def test_imported_class_has_the_pinned_facts(service_id):
    pinned = PINNED[service_id]
    module = _load_deploy(
        pinned["dir"], f"_stack_preset_{service_id.replace('/', '_')}"
    )
    cls = getattr(module, pinned["class"])
    for field in _CLASS_FIELDS:
        assert getattr(cls, field) == pinned[field], f"{service_id}.{field}"


@pytest.mark.parametrize("service_id", sorted(PINNED))
def test_backup_inventory_reads_the_preset_data_path(service_id, monkeypatch):
    # A relative root: the reader skips hidden path parts, and a worktree path
    # (.claude/worktrees/...) has one.
    monkeypatch.chdir(ROOT)
    paths = discover_deployer_data_paths(Path("."))
    assert paths.get(service_id) == PINNED[service_id]["data_path"]


@pytest.mark.parametrize("service_id", sorted(PINNED))
def test_status_task_runs_the_compose_healthcheck(service_id, monkeypatch):
    pinned = PINNED[service_id]
    seen: list[tuple[str, str]] = []
    # deploy.py binds common.common.check_service at import: patch it first.
    monkeypatch.setattr(
        common_module, "check_service", lambda c, svc, cmd: seen.append((svc, cmd))
    )
    module = _load_deploy(pinned["dir"], f"_stack_tasks_{service_id.replace('/', '_')}")
    assert isinstance(module.shared, Collection)
    assert sorted(module.shared.tasks) == ["status"]
    module.shared.tasks["status"].body(Context())
    module.status.body(Context())  # the make_tasks `status` calls the same check
    assert seen == [pinned["status"], pinned["status"]]
    for name in ("pre_compose", "composing", "post_compose", "setup", "sync"):
        assert isinstance(getattr(module, name), Task), name
    # The status command is the compose healthcheck with `$$` unescaped.
    compose = yaml.safe_load((ROOT / pinned["compose_path"]).read_text("utf-8"))
    test = compose["services"][pinned["service"]]["healthcheck"]["test"]
    assert test[1].replace("$$", "$") == pinned["status"][1]


def _tree(body: str) -> ast.Module:
    return ast.parse(
        "class X(PostgresStackDeployer):\n"
        + "".join(f"    {line}\n" for line in body.splitlines())
    )


def test_a_literal_attribute_wins_over_the_preset_in_both_readers():
    tree = _tree("project = 'demo'\ndata_path = '/data/demo/pg-other'")
    assert reg._class_attr(tree, "data_path") == "/data/demo/pg-other"
    assert reg._class_attr(tree, "service_port") == 5432

    class Demo(stack_presets.PostgresStackDeployer):
        project = "demo"
        data_path = "/data/demo/pg-other"

    assert Demo.data_path == "/data/demo/pg-other"
    assert Demo.service_port == 5432
    assert Demo.compose_path == "demo/demo/01.postgres/compose.yaml"


def test_a_preset_without_a_literal_project_fails_closed():
    with pytest.raises(ValueError, match="literal `project`"):
        preset_defaults(_tree("service_port = 1"))
    with pytest.raises(TypeError, match="needs `project`"):

        class NoProject(stack_presets.RedisStackDeployer):
            service_port = 1


def test_a_plain_deployer_gets_no_preset_facts():
    tree = ast.parse("class X(Deployer):\n    project = 'demo'\n")
    assert preset_defaults(tree) == {}
    assert reg._class_attr(tree, "service_port") is None
    assert reg._facet_seq(tree, "probes", ProbeFacet, "x") == ()


def test_invoke_keeps_every_task_name_of_the_three_stacks():
    """The task names before the preset: six deploy tasks and `shared.status`."""
    from tools.loader import ns

    names = set(ns.task_names)
    for prefix in ("fr-postgres", "fr-redis", "ta-postgres"):
        expected = {
            f"{prefix}.{name}"
            for name in (
                "status",
                "pre-compose",
                "composing",
                "post-compose",
                "setup",
                "sync",
                "shared.status",
            )
        }
        assert expected <= names, sorted(expected - names)
        assert {n for n in names if n.startswith(f"{prefix}.")} == expected
