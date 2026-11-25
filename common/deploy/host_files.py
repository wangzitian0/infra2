"""Write a repository file to a host path that a compose bind-mounts (#1438, #1822).

A raw Dokploy compose has no checkout, so a service cannot mount `./file` from the
repository. The service deploy.py writes the file to `${DATA_PATH}/<name>` on the host
instead, and the compose mounts that host path. The file in the service directory stays
the one source: the config hash reads it (`config_hash._host_mount_template`), and the
compose label `${IAC_MOUNT_SHA256_<NAME>:-}` recreates the container when it changes.
"""

from __future__ import annotations

import base64
import posixpath
import re
import shlex
from collections.abc import Callable
from typing import Any

_MODE_RE = re.compile(r"0[0-7]{3}")


def host_file_command(path: str, content: bytes, mode: str = "0644") -> str:
    """Shell command that writes `content` to `path` atomically with file mode `mode`.

    The content travels base64-encoded, so no byte of it is shell syntax. The command
    writes a temporary file in the same directory and renames it, so a reader never
    sees a partial file.
    """
    if not path.startswith("/") or path.endswith("/"):
        raise ValueError(f"host file path must be an absolute file path: {path!r}")
    if not _MODE_RE.fullmatch(mode):
        raise ValueError(f"file mode must be 4 octal digits such as 0644: {mode!r}")
    directory = posixpath.dirname(path)
    tmp = f"{path}.tmp"
    encoded = base64.b64encode(content).decode("ascii")
    return (
        f"mkdir -p {shlex.quote(directory)}"
        f" && printf %s {encoded} | base64 -d > {shlex.quote(tmp)}"
        f" && chmod {mode} {shlex.quote(tmp)}"
        f" && mv -f {shlex.quote(tmp)} {shlex.quote(path)}"
    )


def deliver_host_files(
    on_host: Callable[[str], Any] | None,
    files: dict[str, tuple[bytes, str]],
) -> bool:
    """Write each `{host path: (content, mode)}` with `on_host`. False on the first failure.

    `on_host` is `Deployer._on_host(c, e)`; None (no VPS_HOST) delivers nothing and
    returns False, so the deploy stops instead of mounting a missing file.
    """
    if on_host is None:
        return False
    for path, (content, mode) in files.items():
        result = on_host(host_file_command(path, content, mode))
        if not getattr(result, "ok", False):
            return False
    return True
