"""A fake VPS reached over ssh, with a docker that answers like the real one.

Commands arrive as ``ssh <dest> '<command>'``. The fake lists containers for
``docker ps`` and ``docker ps -a``, restarts the ones that exist, and fails a
``docker restart`` that names a container that does not exist, as the real docker does
(it restarts the existing ones and exits 1). `restart_ok=False` fails every restart. Any other command, such as `psql`, is
recorded with its stdin.
"""

from __future__ import annotations

import shlex


class Run:
    def __init__(self, ok: bool = True, stdout: str = "", stderr: str = ""):
        self.ok, self.failed = ok, not ok
        self.stdout, self.stderr = stdout, stderr

    def __bool__(self) -> bool:
        return self.ok


class DockerHost:
    def __init__(
        self,
        containers=(),
        *,
        running=None,
        list_ok: bool = True,
        restart_ok: bool = True,
        other_ok: bool = True,
        other_stderr: str = "",
    ):
        self.containers = set(containers)
        self.running = set(containers if running is None else running)
        self.list_ok = list_ok
        self.restart_ok = restart_ok
        self.other_ok, self.other_stderr = other_ok, other_stderr
        self.commands: list[str] = []
        self.stdin: list[str] = []
        self.restarted: list[str] = []

    @staticmethod
    def remote(command: str) -> str:
        parts = shlex.split(command)
        return parts[-1] if parts[:1] == ["ssh"] else command

    def run(self, command, in_stream=None, hide=True, warn=True, **kwargs):
        self.commands.append(command)
        self.stdin.append(in_stream.read() if in_stream is not None else "")
        remote = self.remote(command)
        if remote.startswith("docker ps"):
            if not self.list_ok:
                return Run(False, stderr="Cannot connect to the Docker daemon")
            names = self.containers if " -a" in remote else self.running
            return Run(True, stdout="".join(f"{name}\n" for name in sorted(names)))
        if remote.startswith("docker restart"):
            names = shlex.split(remote)[2:]
            missing = [name for name in names if name not in self.containers]
            self.restarted.extend(name for name in names if name in self.containers)
            if missing:
                return Run(False, stderr=f"Error: No such container: {missing[0]}")
            if not self.restart_ok:
                return Run(False, stderr="Error: container is restarting")
            return Run(True)
        return Run(self.other_ok, stderr=self.other_stderr)

    @property
    def restart_commands(self) -> list[str]:
        return [c for c in self.commands if "docker restart" in self.remote(c)]
