"""Deployer presets for the App PostgreSQL and Redis stacks (#1825 Slice 1).

An App data-store ``deploy.py`` sets ``project`` on a preset subclass and exports
``stack_tasks``. The preset derives every other fact from ``project`` through
``common.core.stack_preset_facts``, the one definition that the AST readers
(``common.core.registry``, ``common.backup.verification``) also use.

The preset does not touch ``compose.yaml``: each stack keeps its own file, so the
compiled compose and its CONFIGURATION_SHA256 stay the same.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from invoke import Collection, task

from common.core.stack_preset_facts import PRESET_BASES, health_command, preset_attrs
from common.deploy.deployer import Deployer, make_tasks


_LABELS = {"postgres": "PostgreSQL", "redis": "Redis"}


class _StackDeployer(Deployer):
    """Fill each preset fact that the subclass body does not set."""

    _preset_kind = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "_preset_kind" in cls.__dict__:
            return  # a preset itself, not a stack
        if "project" not in cls.__dict__:
            raise TypeError(f"{cls.__name__}: a stack preset needs `project`")
        for name, value in preset_attrs(cls._preset_kind, cls.project).items():
            if name not in cls.__dict__:
                setattr(cls, name, value)


class PostgresStackDeployer(_StackDeployer):
    """The App PostgreSQL stack in ``<project>/<project>/01.postgres``."""

    _preset_kind = PRESET_BASES["PostgresStackDeployer"]


class RedisStackDeployer(_StackDeployer):
    """The App Redis stack in ``<project>/<project>/02.redis``."""

    _preset_kind = PRESET_BASES["RedisStackDeployer"]


def stack_tasks(
    deployer_cls: type[_StackDeployer], check_service: Callable[..., dict]
) -> dict[str, Any]:
    """The invoke tasks of a stack: the ``make_tasks`` set and a ``shared`` collection.

    ``tools/loader.py`` adds the ``shared`` collection as ``<prefix><service>.shared``,
    the place where the deleted ``shared_tasks.py`` put the ``status`` task.
    The caller gives ``common.common.check_service``: a domain package must not
    import that flat module (common/tests/test_import_boundaries.py).
    """
    kind = deployer_cls._preset_kind
    container = f"{deployer_cls.project}-{kind}"
    command = health_command(kind, deployer_cls.project)

    def status(c):
        return check_service(c, container, command)

    status.__doc__ = f"Check {_LABELS[kind]} health status."
    status_task = task(status)
    shared = Collection()
    shared.add_task(status_task)
    tasks = make_tasks(deployer_cls, SimpleNamespace(status=status_task))
    return {**tasks, "shared": shared}
