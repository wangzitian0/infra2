"""Shared SSH configuration and command builder for out-of-band watchdog and stability report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SshConfig:
    host: str
    user: str
    port: int
    key_path: str


def load_ssh_config(env: Mapping[str, str]) -> SshConfig | None:
    """Return SSH config when every required field is present."""
    host = env.get("INFRA2_WATCHDOG_SSH_HOST", "").strip()
    user = env.get("INFRA2_WATCHDOG_SSH_USER", "").strip()
    key_path = env.get("INFRA2_WATCHDOG_SSH_KEY_PATH", "").strip()
    if not host or not user or not key_path:
        return None
    return SshConfig(
        host=host,
        user=user,
        port=int(env.get("INFRA2_WATCHDOG_SSH_PORT", "") or "22"),
        key_path=key_path,
    )


def _ssh_argv(config: SshConfig, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-i",
        config.key_path,
        "-p",
        str(config.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{config.user}@{config.host}",
        remote_command,
    ]


__all__ = ["SshConfig", "_ssh_argv", "load_ssh_config"]
