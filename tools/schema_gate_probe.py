#!/usr/bin/env python3
"""Read-only probe: can a pre-deploy schema gate actually run against the VPS?

``docs/ssot/ops.standards.md`` Rule 7 says the Pre-Deploy Schema Gate MUST run
before any deploy that destroys or restarts a container with persistent
storage. ``tools/pre_deploy_schema_check.py`` implements it, 27 tests cover it,
and it is wired into nothing — Infra-022 has carried "尚未接入 deploy_v2" since
2026-09-16.

The reason it is not wired in is a real constraint, not an oversight, and this
probe exists to replace guesses about that constraint with observations. The
gate needs two halves that live in different places:

- the **code-side enums** come from the service's own SQLAlchemy metadata, so
  they can only be read inside the application image;
- the **database** is reachable only from the Docker network on the VPS, and
  ``DATABASE_URL`` is not in the compose file at all — vault-agent renders it
  into the *running* container at runtime.

``deploy_v2`` runs on a GitHub-hosted runner with neither (``libs/deploy/
promote.py``: "the only view of the host this tier has (it runs in GitHub
Actions, no ssh)"), which is why calling the gate from there today returns
EXIT_NOT_EVALUATED for every service — measured, both a registered and an
unregistered one.

So before proposing an integration, four things have to be observed rather
than assumed. This probe answers them and changes nothing:

1. is the backend container running, and under what name;
2. can ``DATABASE_URL`` be read from it (the value is never printed — only
   whether it exists and what scheme it carries);
3. does the application image start, and does the service's metadata import
   inside it;
4. does the gate itself run end to end there, and with what exit code.

Exit codes mirror the gate's own vocabulary: 0 every probe answered, 2 the
probe could not run (no SSH, no container). It never returns 1 — a probe has
no verdict to give.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

PROBE_OK = 0
PROBE_INFRA = 2

# The gate runs inside the image, so its script has to get there somehow.
# Piping it over stdin to `python -` avoids baking an infra2 tool into an
# application image or mounting a path that only exists on the runner.
GATE_SCRIPT = "tools/pre_deploy_schema_check.py"


def ssh_argv(env: dict[str, str]) -> list[str] | None:
    """The ssh prefix, or None when the credentials are not configured.

    Same variables the watchdog and the bootstrap updater already use, so this
    adds no new secret and no new access path.
    """
    host = (env.get("INFRA2_WATCHDOG_SSH_HOST") or "").strip()
    user = (env.get("INFRA2_WATCHDOG_SSH_USER") or "").strip()
    key = (env.get("INFRA2_WATCHDOG_SSH_KEY_PATH") or "").strip()
    if not (host and user and key):
        return None
    port = (env.get("INFRA2_WATCHDOG_SSH_PORT") or "22").strip()
    return [
        "ssh",
        "-i",
        key,
        "-p",
        port,
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        f"{user}@{host}",
    ]


def run(
    argv: list[str], *, stdin: str = "", timeout: int = 120
) -> tuple[int, str, str]:
    """Never raises. A probe that dies with a traceback has reported nothing.

    ``subprocess.run`` raises ``TimeoutExpired`` when the remote hangs and
    ``OSError`` when ssh is not installed. Both would leave Python exiting 1,
    a code this tool's vocabulary does not have, and a stack trace where a
    structured answer belongs.
    """
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return PROBE_INFRA, "", f"timed out after {timeout}s"
    except OSError as exc:
        return PROBE_INFRA, "", f"could not execute {argv[0]!r}: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def remote(ssh: list[str], command: str, *, stdin: str = "", timeout: int = 120):
    return run([*ssh, command], stdin=stdin, timeout=timeout)


def redact(text: str, *secrets: str) -> str:
    """Remove secret values this process holds from text about to be printed.

    Not pattern matching. The probe knows the exact DATABASE_URL it read, so
    the set of strings to remove is closed, and removing them is exact rather
    than a guess about what a secret looks like.

    It matters because raw stderr is reported for diagnosis, and a database
    driver's exception message routinely carries the whole connection string.
    These logs are readable by anyone who can read the repository.
    """
    for secret in secrets:
        # Two characters would redact half the alphabet out of the diagnosis.
        if secret and len(secret) > 3:
            text = text.replace(secret, "***")
    return text


def secrets_in(url: str) -> tuple[str, ...]:
    """The URL and its password, both worth removing on their own.

    A driver may echo the whole URL, or quote only the credential it failed to
    authenticate with.
    """
    if not url:
        return ()
    parts = [url]
    if "://" in url and "@" in url:
        userinfo = url.split("://", 1)[1].rsplit("@", 1)[0]
        if ":" in userinfo:
            parts.append(userinfo.split(":", 1)[1])
    return tuple(p for p in parts if p)


def scheme_of(url: str) -> str:
    """The scheme and host shape only — never the credentials."""
    if not url:
        return ""
    head = url.split("://", 1)
    if len(head) != 2:
        return "malformed"
    return head[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", default="finance_report/app")
    parser.add_argument("--container", default="finance_report-backend")
    parser.add_argument("--network", default="dokploy-network")
    parser.add_argument(
        "--image",
        default="ghcr.io/wangzitian0/finance_report-backend:latest",
        help="image to probe; the real gate would use the ref being deployed",
    )
    parser.add_argument("--app-path", default="", help="passed through to the gate")
    args = parser.parse_args()

    report: dict[str, object] = {"service": args.service, "probes": {}}
    probes: dict[str, object] = report["probes"]  # type: ignore[assignment]

    ssh = ssh_argv(os.environ)
    if ssh is None:
        print(
            json.dumps(
                {
                    "verdict": "INFRA",
                    "reason": "no SSH credentials: INFRA2_WATCHDOG_SSH_HOST / _USER / "
                    "_KEY_PATH must all be set",
                },
                indent=2,
            )
        )
        return PROBE_INFRA

    code, out, err = remote(ssh, "docker --version")
    # The only output printed unscrubbed, and the only one that can be:
    # it happens before any credential has been read, so there is nothing
    # yet to scrub, and `docker --version` carries none.
    probes["docker_reachable"] = {"exit": code, "version": out, "stderr": err[:200]}
    if code != 0:
        report["verdict"] = "INFRA"
        report["reason"] = "ssh reached nothing that can run docker"
        print(json.dumps(report, indent=2))
        return PROBE_INFRA

    # 1. the container, by name prefix so the env suffix does not have to be known
    # No grep and no `|| true`. Piping into grep merges two exit codes into
    # one, and `|| true` then discards it: `docker ps` failing on a permission
    # error or a dead daemon would arrive here as an empty list and be
    # reported as "no container is running", which is a different fact. The
    # listing is filtered in Python so docker's own verdict survives.
    code, out, err = remote(ssh, "docker ps --format '{{.Names}}\t{{.Image}}'")
    if code != 0:
        report["verdict"] = "INFRA"
        report["reason"] = f"docker ps failed (exit {code}): {err[:200]}"
        print(json.dumps(report, indent=2))
        return PROBE_INFRA
    running = [
        line.split("\t")
        for line in out.splitlines()
        if line.strip() and args.container in line.split("\t")[0]
    ]
    probes["running_containers"] = running
    # `--container` is a name prefix: the env suffix is not known here, and
    # prod, staging and every live preview can share it. Picking the first
    # would read DATABASE_URL from whichever docker listed first and report it
    # as the answer, so ambiguity is refused and the caller narrows it.
    if len(running) != 1:
        report["verdict"] = "INFRA"
        report["reason"] = (
            f"{len(running)} running containers match {args.container!r}"
            + (
                f": {[r[0] for r in running]}. Narrow it with --container."
                if running
                else ", so there is nothing to read DATABASE_URL from."
            )
        )
        print(json.dumps(report, indent=2))
        return PROBE_INFRA
    container = running[0][0]

    # 2. DATABASE_URL, existence and scheme only
    code, db_url, err = remote(
        ssh, f"docker exec {shlex.quote(container)} printenv DATABASE_URL"
    )
    # Held for the end-to-end run below and, just as importantly, to scrub
    # every piece of output this process prints from here on.
    secrets = secrets_in(db_url)
    probes["database_url"] = {
        "exit": code,
        "readable": bool(db_url),
        "scheme": scheme_of(db_url),
        "source": f"docker exec {container} printenv",
        "stderr": redact(err, *secrets)[:200],
    }

    # 3. the image: does it start, where does it work from, does the metadata
    #    import. No `|| true` on either: appending it makes the remote shell
    #    exit 0 whatever happened, so `exit` would read 0 for an image that
    #    never started -- erasing the one thing these probes are for. `run()`
    #    returns the code without raising, so there is nothing to swallow.
    code, out, err = remote(
        ssh,
        f"docker run --rm --entrypoint sh {shlex.quote(args.image)} -c "
        "'pwd; python3 -c \"import sys; print(sys.version.split()[0])\"'",
        timeout=300,
    )
    probes["image_starts"] = {
        "exit": code,
        "stdout": redact(out, *secrets)[:300],
        "stderr": redact(err, *secrets)[:300],
    }

    code, out, err = remote(
        ssh,
        f"docker run --rm --entrypoint sh {shlex.quote(args.image)} -c "
        "'python3 -c \"import src.orm_registry, src.database; "
        "print(len(src.database.Base.metadata.tables))\"'",
        timeout=300,
    )
    probes["metadata_imports"] = {
        "exit": code,
        "tables": redact(out, *secrets)[:300],
        "stderr": redact(err, *secrets)[:300],
    }

    # 4. the gate itself, end to end, with the script piped in over stdin
    try:
        with open(GATE_SCRIPT, encoding="utf-8") as handle:
            gate_source = handle.read()
    except OSError as exc:
        # Skipping the one probe that answers the actual question, and still
        # reporting PROBED with exit 0, would tell the caller the end-to-end
        # check ran.
        report["verdict"] = "INFRA"
        report["reason"] = f"cannot read {GATE_SCRIPT}: {exc}"
        print(json.dumps(report, indent=2))
        return PROBE_INFRA

    if gate_source:
        app_path = f" --app-path {shlex.quote(args.app_path)}" if args.app_path else ""
        command = (
            f"DB=$(docker exec {shlex.quote(container)} printenv DATABASE_URL) && "
            f"docker run --rm -i --network {shlex.quote(args.network)} "
            f'-e DATABASE_URL="$DB" --entrypoint python3 {shlex.quote(args.image)} '
            f"- --service {shlex.quote(args.service)}{app_path}"
        )
        code, out, err = remote(ssh, command, stdin=gate_source, timeout=300)
        probes["gate_end_to_end"] = {
            "exit": code,
            "verdict_line": redact(out.splitlines()[-1] if out else "", *secrets)[:400],
            # Scrubbed, not merely truncated: a driver's exception message
            # routinely carries the whole connection string, and these logs are
            # readable by anyone who can read the repository.
            "stderr": redact(err, *secrets)[:400],
        }

    report["verdict"] = "PROBED"
    print(json.dumps(report, indent=2))
    return PROBE_OK


if __name__ == "__main__":
    sys.exit(main())
