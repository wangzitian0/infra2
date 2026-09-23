"""Pre-deploy schema gate: runs ``tools/pre_deploy_schema_check.py`` inside the app's
own published image, against the target env's live database (#698, SSOT
``ops.standards.md`` Rule 7 / Infra-022 TODOWRITE:20).

Why this needs SSH to the VPS rather than running in-process: ``pre_deploy_schema_check``
imports the app's own SQLAlchemy metadata (``src.database:Base.metadata``) — a real
answer requires the app's own dependencies and code AT THE EXACT COMMIT being deployed,
which only exist inside its published OCI image, not in infra2's own Python environment
(TODOWRITE:20 — "在 infra2 的环境里跑不出正确结果"). Physically checked 2026-09-22:

- ``deploy_v2``'s own process (a GitHub Actions runner) has neither a docker daemon nor
  network reachability to the VPS's ``dokploy-network`` — only the ``bootstrap`` job in
  ``.github/workflows/deploy.yml`` (iac-runner self-update) carries
  ``INFRA2_WATCHDOG_SSH_*``, not the ``deploy`` job.
- The iac-runner container IS reachable to the app's Postgres over ``dokploy-network``
  (``docker exec iac-runner python3 -c "socket.gethostbyname('finance_report-postgres-staging')"``
  resolves), but has no docker socket — ``docker inspect iac-runner`` shows only the
  ``secrets``/``workspace``/``host_ssh`` mounts; ``bootstrap/06.iac_runner/README.md``'s
  "Read-only docker socket mount" line does not match the live container and is stale.
- Only the VPS host itself runs the docker daemon AND sits on ``dokploy-network``.

So this SSHes to the VPS (the same ``INFRA2_WATCHDOG_SSH_*`` convention
``tools/secrets_reconcile_check.py`` and ``libs/vault_self_refresh_audit.py`` already
use) and:

1. Reads ``DATABASE_URL`` from the CURRENTLY RUNNING vault-agent sidecar's rendered
   ``/vault/secrets/.env`` — the same value the about-to-be-replaced old container
   already uses to reach this exact database (host/credentials do not change across an
   app-code deploy within one env; verified live against finance_report staging+prod).
2. Runs a throwaway container FROM THE IMAGE ABOUT TO BE DEPLOYED, entrypoint
   overridden to plain ``python3`` (the image's own entrypoint runs migrations and
   starts the app server — NEVER let it run un-overridden here: an earlier live probe
   without ``--entrypoint`` accidentally booted a full extra app instance and ran
   migrations against staging before being caught and torn down), attached to
   ``dokploy-network``, piping ``tools/pre_deploy_schema_check.py`` in over stdin
   (``python3 -``) so no volume mount / no checkout on the remote side is needed.

Fail-closed: exit 0 is the only pass. Exit 1 (discrepancy) and 3 (NOT EVALUATED —
missing DB URL / a code-side import failure, #718 review) both block, and so does
anything else — an SSH/docker transport failure, an unparseable result — because a
check that could not run to a real verdict has not verified anything (the same
fail-closed principle Rule 7 states for a DB query failure).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from libs.deploy.in_service import container_names
from libs.service_registry import REPO_ROOT


@dataclass(frozen=True)
class EnumSource:
    """Where a service's code-side enum truth lives.

    ``metadata`` names a SQLAlchemy ``MetaData`` as ``module:attribute.path``; every
    native enum column type registered on it is one Postgres enum type (its ``name``)
    with its labels (``enums``). ``imports`` are imported first, for their side effect
    of registering every mapped class on that metadata. ``app_path`` says which
    directory of the service's checkout has to be on ``sys.path``.
    """

    metadata: str
    imports: tuple[str, ...] = ()
    app_path: str = ""


# finance_report's backend is the ``src`` package under apps/backend of the
# finance_report repository — there is no ``finance_report.models.enums`` (the infra2
# ``finance_report`` package is deploy config). Its mapped classes register on
# ``src.database.Base.metadata`` once ``src.orm_registry`` is imported, and every
# Postgres enum there carries an explicit ``name=`` (``account_type_enum``, migration
# 0007) that no class-name derivation reproduces.
ENUM_SOURCES: dict[str, EnumSource] = {
    "finance_report/app": EnumSource(
        metadata="src.database:Base.metadata",
        imports=("src.orm_registry",),
        app_path="<finance_report checkout>/apps/backend",
    ),
}

_SCHEMA_CHECK_SCRIPT = (
    Path(__file__).resolve().parents[2] / "tools" / "pre_deploy_schema_check.py"
)

# Only the backend image carries the ORM (`src.database`) — the frontend image also
# listed in ServiceSpec.image_repositories does not. Explicit rather than
# "image_repositories[0]": that ordering is incidental wiring for the artifact-readiness
# wait, not a contract about which repository holds the code-side enum truth.
_BACKEND_IMAGE_REPOSITORY: dict[str, str] = {
    "finance_report/app": "ghcr.io/wangzitian0/finance_report-backend",
}

_DOKPLOY_NETWORK = "dokploy-network"
_VAULT_AGENT_SERVICE_KEY = "vault-agent"
_SSH_CONTROL_PATH = "/tmp/infra2-schema-gate-%r@%h:%p"


class SchemaGateError(RuntimeError):
    """The pre-deploy schema gate blocked, or could not run to a verdict."""


def gate_applies(service: str) -> bool:
    """Whether this service has a registered code-side enum source at all.

    Only a service ``tools.pre_deploy_schema_check`` actually knows how to evaluate is
    gated here — every other service is unaffected by this integration (a service with
    persistent enums needs registering in
    ``tools.pre_deploy_schema_check.ENUM_SOURCES`` first, same as
    ``docs/onboarding/07.new-service-sop.md`` already documents). Expanding coverage to
    another service is separate follow-up work, not this wiring.
    """
    return service in ENUM_SOURCES


def _ssh_args(host: str) -> list[str]:
    """The watchdog SSH convention (``tools/secrets_reconcile_check.py``,
    ``libs/vault_self_refresh_audit.py``): explicit key/port/user when
    ``INFRA2_WATCHDOG_SSH_*`` are provisioned (a GitHub Actions runner has no ambient
    identity/known_hosts trust for the VPS), ambient SSH config otherwise (invoked from
    inside the VPS, where root's default identity already trusts itself).
    """
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60s",
        "-o",
        f"ControlPath={_SSH_CONTROL_PATH}",
    ]
    key_path = os.environ.get("INFRA2_WATCHDOG_SSH_KEY_PATH", "").strip()
    if key_path:
        args += [
            "-i",
            key_path,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
    port = os.environ.get("INFRA2_WATCHDOG_SSH_PORT", "").strip()
    if port:
        args += ["-p", port]
    user = os.environ.get("INFRA2_WATCHDOG_SSH_USER", "").strip() or "root"
    return [*args, f"{user}@{host}"]


def _ssh_host() -> str:
    host = (
        os.environ.get("INFRA2_WATCHDOG_SSH_HOST", "").strip()
        or os.environ.get("VPS_HOST", "").strip()
    )
    if not host:
        raise SchemaGateError(
            "pre-deploy schema gate: no VPS host to SSH to "
            "(INFRA2_WATCHDOG_SSH_HOST or VPS_HOST)"
        )
    return host


def _vault_agent_container(service: str, compose_path: str, env_suffix: str) -> str:
    text = (REPO_ROOT / compose_path).read_text(encoding="utf-8")
    names = container_names(text, env_suffix)
    name = names.get(_VAULT_AGENT_SERVICE_KEY)
    if not name:
        raise SchemaGateError(
            f"{service}: {compose_path} declares no {_VAULT_AGENT_SERVICE_KEY!r} "
            "service to read the deploy-time DATABASE_URL from"
        )
    return name


def _read_database_url(
    host: str, vault_agent_container: str, *, runner, timeout: float
) -> str:
    command = (
        f"docker exec {shlex.quote(vault_agent_container)} "
        "sh -lc 'cat /vault/secrets/.env 2>/dev/null'"
    )
    result = runner(
        [*_ssh_args(host), command], text=True, capture_output=True, timeout=timeout
    )
    if result.returncode != 0:
        # `result.stdout`/`result.stderr` here are whatever the remote side captured
        # for a `cat /vault/secrets/.env` invocation — the RENDERED SECRETS FILE itself
        # (DATABASE_URL, and possibly more the template renders). Unlike
        # ``run_schema_gate``'s own BLOCKED message (that one is the check script's
        # diagnostic text, which never contains a credential), this command's captured
        # output must NEVER be echoed into an exception a CI log could carry — one
        # failure here would otherwise print a secret straight into GitHub Actions'
        # public step log. Identify the failing step + exit code only; no content.
        raise SchemaGateError(
            "pre-deploy schema gate: could not read the rendered secrets from "
            f"{vault_agent_container} on {host} (ssh/docker exit {result.returncode}) "
            "-- command output withheld: this reads a rendered secrets file, and a "
            "failure must not risk echoing its content into a log"
        )
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SchemaGateError(
        f"pre-deploy schema gate: {vault_agent_container} on {host} rendered no "
        "DATABASE_URL to check the new image's ORM against"
    )


_DB_PASSWORD_RE = re.compile(r"://([^:@\s]+):([^@\s]+)@")


def _sanitize_db_url(text: str) -> str:
    """Mask credentials in database URLs (e.g. postgresql://user:pass@host -> postgresql://user:******@host)."""
    return _DB_PASSWORD_RE.sub(r"://\1:******@", text)


def _run_remote_check(
    host: str,
    *,
    image: str,
    service: str,
    database_url: str,
    runner,
    timeout: float,
) -> subprocess.CompletedProcess:
    docker_cmd = (
        f"docker run --rm -i --network {_DOKPLOY_NETWORK} "
        "--memory=1g --cpus=1 --entrypoint python3 "
        f"-e DATABASE_URL={shlex.quote(database_url)} "
        f"{shlex.quote(image)} - --service {shlex.quote(service)}"
    )
    script = _SCHEMA_CHECK_SCRIPT.read_text(encoding="utf-8")
    return runner(
        [*_ssh_args(host), docker_cmd],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def run_schema_gate(
    service: str,
    *,
    compose_path: str,
    env_suffix: str,
    image_ref: str,
    host: str | None = None,
    runner=subprocess.run,
    timeout: float = 300,
) -> str:
    """Fail-closed pre-deploy schema gate; returns the ``ROLLBACK_CLASS`` on a pass.

    Raises :class:`SchemaGateError` on ANY non-zero outcome — a discrepancy (exit 1),
    NOT EVALUATED (exit 3, #718 review: missing DB URL / a code-side import failure is
    ALSO blocking, never a silent pass), or an SSH/docker transport failure. There is no
    caller-visible distinction between these: every one of them means "do not proceed."
    Callers only invoke this when :func:`gate_applies` is true.
    """
    image = _BACKEND_IMAGE_REPOSITORY.get(service)
    if not image:
        raise SchemaGateError(
            f"{service}: pre-deploy schema gate has no registered backend image "
            "repository to run the check inside "
            "(libs.deploy.schema_gate._BACKEND_IMAGE_REPOSITORY)"
        )
    resolved_host = host or _ssh_host()
    vault_agent = _vault_agent_container(service, compose_path, env_suffix)
    database_url = _read_database_url(
        resolved_host, vault_agent, runner=runner, timeout=timeout
    )
    result = _run_remote_check(
        resolved_host,
        image=f"{image}:{image_ref}",
        service=service,
        database_url=database_url,
        runner=runner,
        timeout=timeout,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    # Parsed BEFORE the returncode check, on purpose: pre_deploy_schema_check.py's
    # main() prints ROLLBACK_CLASS on stdout for BOTH its passing and its blocked exit
    # path (everything except NOT EVALUATED, exit 3, which never computed a
    # comparison) -- the blocked case is exactly when an operator most wants to know
    # the class without re-running the check by hand. run_schema_gate still raises on
    # block (a blocked deploy must not proceed), but folds the class into the
    # exception message so it reaches the deploy log either way.
    rollback_class = ""
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("ROLLBACK_CLASS: "):
            rollback_class = line.split(":", 1)[1].strip()
    if result.returncode != 0:
        suffix = f" (ROLLBACK_CLASS: {rollback_class})" if rollback_class else ""
        sanitized_output = _sanitize_db_url(output.strip()[-2000:])
        raise SchemaGateError(
            f"pre-deploy schema gate BLOCKED {service} (exit {result.returncode})"
            f"{suffix}: {sanitized_output}"
        )
    if not rollback_class:
        # exit 0 with no parseable ROLLBACK_CLASS is not a real answer -- treat an
        # unparseable pass as blocking rather than trusting it (Rule 7's "never a
        # silent pass" applies to OUR parsing failures too, not only the script's own).
        raise SchemaGateError(
            f"pre-deploy schema gate: {service} exited 0 but printed no ROLLBACK_CLASS "
            f"line: {output.strip()[-2000:]}"
        )
    return rollback_class
