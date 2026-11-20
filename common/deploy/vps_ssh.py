"""SSH to the VPS: one place for the host, user, key and the remote command runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_VPS_HOST = "103.214.23.41"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "id_ed25519_infra"


def ssh_cmd(
    command: str, host: str, user: str, key_path: str, port: int = 22
) -> tuple[int, str, str]:
    ssh_args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-p",
        str(port),
    ]
    if key_path and os.path.exists(key_path):
        ssh_args.extend(["-i", key_path])
    ssh_args.append(f"{user}@{host}")
    ssh_args.append(command)
    res = subprocess.run(ssh_args, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def resolve_ssh_credentials() -> tuple[str, str, str, int]:
    host = os.environ.get("INFRA2_WATCHDOG_SSH_HOST", "").strip() or DEFAULT_VPS_HOST
    user = os.environ.get("INFRA2_WATCHDOG_SSH_USER", "").strip() or "root"
    key_path = os.environ.get("INFRA2_WATCHDOG_SSH_KEY_PATH", "").strip()
    if not key_path and DEFAULT_SSH_KEY.exists():
        key_path = str(DEFAULT_SSH_KEY)
    port = int(os.environ.get("INFRA2_WATCHDOG_SSH_PORT", "") or "22")
    return host, user, key_path, port


def ssh_script(
    script: str, host: str, user: str, key_path: str, port: int = 22, timeout: int = 600
) -> tuple[int, str, str]:
    """Run `script` through `bash -s` on stdin. No prompt, no TTY, bounded run time.

    The script can carry a Vault token, so this call checks the host key: a first
    connection records it, and a changed key fails the call (no `StrictHostKeyChecking=no`).
    """
    ssh_args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        str(port),
    ]
    if key_path and os.path.exists(key_path):
        ssh_args.extend(["-i", key_path])
    ssh_args.extend([f"{user}@{host}", "bash -s"])
    try:
        res = subprocess.run(
            ssh_args, input=script, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh script timed out after {timeout}s"
    return res.returncode, res.stdout, res.stderr
