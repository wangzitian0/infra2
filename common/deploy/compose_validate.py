"""Validate stack compose files with ``docker compose config`` (infra2#1884).

The CI job ``validate-compose`` and ``tools/ci_runner.py compose`` both use this module.

Rules:

1. A plain file is validated as its source file. The command is
   ``docker compose -f <file> config``, the same command as before ``x-infra2``.
2. An ``x-infra2`` file is validated as its compiled text
   (``compose_compile.load_stack(path).compiled``), the text that the deploy uploads. The
   compiled text goes to ``<work dir>/<repository-relative path>``, so the project
   directory has the same name and the project name does not change. A raw-eligible
   stack has no path relative to the checkout, so the directory change has no effect.
3. ``CI_COMPOSE_ENV`` holds the dummy values for the deploy variables. It is the one
   definition for CI and for ``ci_runner``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from common.core.constants import REPO_ROOT
from common.deploy.compose_compile import ComposeSourceError, load_stack

COMPOSE_CMD: tuple[str, ...] = ("docker", "compose")

CI_COMPOSE_ENV: dict[str, str] = {
    "DATA_PATH": "/tmp/infra2-data",
    "ENV": "ci",
    "ENV_SUFFIX": "-ci",
    "ENV_DOMAIN_SUFFIX": "ci",
    "INTERNAL_DOMAIN": "internal.example.test",
    "VAULT_ADDR": "http://vault.example.test:8200",
    "FEISHU_APP_ID": "dummy-feishu-app-id",
    "FEISHU_APP_SECRET": "dummy-feishu-app-secret",
    "FEISHU_VERIFICATION_TOKEN": "dummy-feishu-verification-token",
    "FEISHU_ENCRYPT_KEY": "dummy-feishu-encrypt-key",
    "LLM_API_KEY": "dummy-llm-api-key",
    "IMAGE_TAG": "0000000",
    "IMAGE_DIGEST": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "RELEASE_ID": "release-manifest:0000000000000000000000000000000000000000000000000000000000000000",
    "CONFIGURATION_SHA256": "0000000000000000000000000000000000000000000000000000000000000000",
    "CAPTURE_APPROVED_BY": "ci:compose-validation",
    "TA_POSTGRES_PORT": "15432",
    "DAGSTER_WEBSERVER_PORT": "13001",
    "OP_CONNECT_TOKEN": "dummy-op-connect-token",
    "OP_VAULT_ID": "dummy-op-vault-id",
    "OP_ITEM_ID": "dummy-op-item-id",
    "REDIS_PASSWORD": "dummy-redis-password",
    "POSTGRES_ADMIN_PASSWORD": "dummy-postgres-admin-password",
    "TODO_POSTGRES_PASSWORD": "dummy-todo-postgres-password",
    "S3_ACCESS_KEY": "dummy-storage-access-key",
    "S3_SECRET_KEY": "dummy-storage-secret-key",
}


def config_command(
    compose_path: str | Path,
    work_dir: Path,
    *,
    compose_cmd: Sequence[str] = COMPOSE_CMD,
) -> list[str]:
    """The ``docker compose config`` command that validates one stack compose file.

    ``compose_path`` is relative to the repository root or absolute. An ``x-infra2`` file
    writes its compiled text below ``work_dir``. Raises ``ComposeSourceError`` when the
    ``x-infra2`` file does not compile.
    """
    path = Path(compose_path)
    stack = load_stack(path)
    if not stack.x_infra2:
        return [*compose_cmd, "-f", str(compose_path), "config"]
    disk = path if path.is_absolute() else REPO_ROOT / path
    try:
        rel = disk.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        rel = Path(disk.parent.name) / disk.name
    target = work_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(stack.compiled, encoding="utf-8")
    return [*compose_cmd, "-f", str(target), "config"]


def validate(
    paths: Sequence[str | Path],
    *,
    compose_cmd: Sequence[str] = COMPOSE_CMD,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> list[str]:
    """Run ``docker compose config`` for each file and return the files that fail.

    The environment is the process environment with ``CI_COMPOSE_ENV`` on top. Each
    file prints one ``Validating:`` line; ``docker compose`` stdout is discarded.
    """
    run = run or subprocess.run
    env = {**os.environ, **CI_COMPOSE_ENV}
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="compose-validate-") as work:
        for compose_path in paths:
            print(f"Validating: {compose_path}", flush=True)
            try:
                cmd = config_command(compose_path, Path(work), compose_cmd=compose_cmd)
            except ComposeSourceError as exc:
                print(f"ERROR: {compose_path}: {exc}", file=sys.stderr)
                failed.append(str(compose_path))
                continue
            if run(cmd, env=env, stdout=subprocess.DEVNULL).returncode != 0:
                failed.append(str(compose_path))
    return failed


__all__ = ["CI_COMPOSE_ENV", "COMPOSE_CMD", "config_command", "validate"]
