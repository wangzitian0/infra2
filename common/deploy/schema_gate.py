"""Pre-deploy schema gate: runs ``python -m infra2_sdk.storage check-schema`` inside the
app's own published image, against the target env's live database (#698, #1824, SSOT
``ops.standards.md`` Rule 7 / Infra-022 TODOWRITE:20).

Why this needs SSH to the VPS rather than running in-process: the check imports the
app's own SQLAlchemy metadata (``src.database:Base.metadata``). A real answer requires
the app's own dependencies and code AT THE EXACT COMMIT being deployed, which only exist
inside its published OCI image, not in infra2's own Python environment
(TODOWRITE:20). Physically checked 2026-09-22:

- The GitHub Actions deploy job has no docker daemon and no network path to the VPS's
  ``dokploy-network``. It holds the ``INFRA2_WATCHDOG_SSH_*`` key (``deploy.yml`` and
  ``reconcile-iac-inputs.yml``) and SSHes to the VPS.
- The iac-runner container has no docker socket (``bootstrap/06.iac_runner/README.md``).
- Only the VPS host itself runs the docker daemon AND sits on ``dokploy-network``.

So this SSHes to the VPS (the ``INFRA2_WATCHDOG_SSH_*`` convention that
``tools/secrets_reconcile_check.py`` and ``common/security/vault_self_refresh_audit.py``
also use) and:

1. Reads ``DATABASE_URL`` from the CURRENTLY RUNNING vault-agent sidecar's rendered
   ``/vault/secrets/.env``. The about-to-be-replaced old container uses the same value
   to reach this exact database (host and credentials do not change across an
   app-code deploy within one env).
2. Runs a throwaway container FROM THE IMAGE ABOUT TO BE DEPLOYED, entrypoint
   overridden to plain ``python3`` (the image's own entrypoint runs migrations and
   starts the app server -- NEVER let it run un-overridden here: an earlier live probe
   without ``--entrypoint`` booted a full extra app instance and ran migrations against
   staging), attached to ``dokploy-network``. The container runs ``_LAUNCHER``, which
   calls the SDK ``schema_check`` CLI with the service's ``SchemaCheckSpec`` and an
   explicit ``--db-url``. The URL travels on stdin, so it is not in any process
   argument list on the VPS.

The image owns the check code: infra2 sends only metadata names. An image whose
infra2-sdk lacks ``runtime.schema_check`` cannot evaluate. ``_LAUNCHER`` then exits
``EXIT_NOT_EVALUATED`` and the gate blocks.

Fail-closed: exit 0 with a ``ROLLBACK_CLASS`` line is the only pass. Exit 1 (blocked),
exit 3 (not evaluated) and every other result -- an SSH or docker transport failure, a
timeout, an unparseable pass -- raise :class:`SchemaGateError`.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass

from common.deploy.in_service import container_names

# The SDK schema check exit codes (infra2_sdk.storage check-schema). 2 is argparse's
# usage error. Every code except EXIT_OK blocks the deploy.
EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_NOT_EVALUATED = 3

VERDICT_BLOCKED = "BLOCKED"
VERDICT_NOT_EVALUATED = "NOT EVALUATED"
VERDICT_NO_VERDICT = "NO VERDICT"


@dataclass(frozen=True)
class SchemaCheckSpec:
    """Where one service's code-side enum truth lives, as the SDK CLI options name it.

    ``image_repository`` is the image that carries the ORM. ``metadata`` is a SQLAlchemy
    ``MetaData`` as ``module:attribute.path``. ``imports`` are imported first, so that
    every mapped class registers on that metadata. The image WORKDIR must put the
    ``metadata`` package on ``sys.path`` (``python3 -c`` puts the working directory
    first).
    """

    image_repository: str
    metadata: str
    imports: tuple[str, ...] = ()


# The single declaration of the gated services. The finance_report image ships no file
# that declares its metadata, so infra2 keeps the declaration (#1824 follow-up: the
# service declares it in its own repository). Only the backend image carries the ORM;
# the frontend image in ServiceSpec.image_repositories does not. Every Postgres enum in
# src.database:Base.metadata has an explicit ``name=`` (``account_type_enum``).
_SCHEMA_CHECK_SPECS: dict[str, SchemaCheckSpec] = {
    "finance_report/app": SchemaCheckSpec(
        image_repository="ghcr.io/wangzitian0/finance_report-backend",
        metadata="src.database:Base.metadata",
        imports=(
            "src.counter.extension.sql",
            "src.identity.extension.sql",
            "src.orm_registry",
            "src.platform.extension.sql",
        ),
    ),
}

# Runs in the target image as ``python3 -c _LAUNCHER <spec options>``. It reads the
# database URL from stdin and calls the SDK CLI with an explicit ``--db-url``. An image
# This launcher tries ``infra2_sdk.storage`` (`check-schema` subcommand, v4.10+)
# first, then falls back to ``infra2_sdk.runtime.schema_check`` (v4.9.x). An image
# without either exits EXIT_NOT_EVALUATED. Any other import failure, and an exception
# that escapes ``main``, also exit EXIT_NOT_EVALUATED: no comparison ran, so the result
# is not BLOCKED. The return code of ``main`` (0, 1 or 3) and SystemExit pass unchanged.
_LAUNCHER = f"""\
import contextlib
import importlib
import io
import json
import sys
_use_storage = False
try:
    from infra2_sdk import storage
    _use_storage = True
except ImportError:
    storage = None
except Exception as exc:
    print(
        "NOT EVALUATED - deploy blocked: infra2_sdk.storage "
        "does not import: " + repr(exc),
        file=sys.stderr,
    )
    sys.exit({EXIT_NOT_EVALUATED})
schema_check = None
if not _use_storage:
    try:
        from infra2_sdk.runtime import schema_check
    except ImportError as exc:
        print(
            "NOT EVALUATED - deploy blocked: the image has neither "
            "infra2_sdk.storage nor infra2_sdk.runtime.schema_check "
            "(the image SDK is too old): " + repr(exc),
            file=sys.stderr,
        )
        sys.exit({EXIT_NOT_EVALUATED})
    except Exception as exc:
        print(
            "NOT EVALUATED - deploy blocked: infra2_sdk.runtime.schema_check "
            "does not import: " + repr(exc),
            file=sys.stderr,
        )
        sys.exit({EXIT_NOT_EVALUATED})
db_url = sys.stdin.readline().strip()
try:
    if _use_storage:
        rest = []
        imports = []
        argv = sys.argv[1:]
        idx = 0
        while idx < len(argv):
            arg = argv[idx]
            if arg == "--import":
                if idx + 1 >= len(argv):
                    raise ValueError("--import requires a module name")
                imports.append(argv[idx + 1])
                idx += 2
            elif arg.startswith("--import="):
                imports.append(arg.split("=", 1)[1])
                idx += 1
            else:
                rest.append(arg)
                idx += 1
        for mod in imports:
            try:
                importlib.import_module(mod)
            except Exception as exc:
                print(
                    f"NOT EVALUATED - deploy blocked: failed to import {{mod}}: " + repr(exc),
                    file=sys.stderr,
                )
                sys.exit({EXIT_NOT_EVALUATED})
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                if callable(getattr(storage, "main", None)):
                    code = storage.main(["check-schema", *rest, "--db-url", db_url, "--json"])
                else:
                    import runpy

                    old_argv = sys.argv
                    sys.argv = [
                        "infra2_sdk.storage",
                        "check-schema",
                        *rest,
                        "--db-url",
                        db_url,
                        "--json",
                    ]
                    sys.modules.pop("infra2_sdk.storage", None)
                    try:
                        try:
                            runpy.run_module(
                                "infra2_sdk.storage",
                                run_name="__main__",
                                alter_sys=True,
                            )
                            code = 0
                        except SystemExit as exc:
                            code = (
                                int(exc.code)
                                if isinstance(exc.code, int)
                                else (1 if exc.code else 0)
                            )
                    finally:
                        sys.argv = old_argv
        except BaseException:
            raw_out = buf.getvalue()
            if raw_out:
                sys.stdout.write(raw_out)
            raise
        raw_out = buf.getvalue()
        stripped = raw_out.strip()
        payload = None
        if stripped.startswith("{{"):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and "rollback_class" in parsed:
                payload = parsed
        if payload is not None:
            label = payload.get("label") or ""
            prefix = f"[{{label}}] " if label else ""
            print(
                f"{{prefix}}ROLLBACK_CLASS: {{payload['rollback_class']}} "
                f"pending={{payload.get('migrations_pending')}}"
            )
            if code != 0:
                details = {{
                    k: payload[k]
                    for k in (
                        "dropped_tables",
                        "dropped_columns",
                        "enum_discrepancies",
                        "precondition_failures",
                        "new_tables",
                        "additive_columns",
                    )
                    if payload.get(k)
                }}
                print(
                    "SCHEMA_DIFF: " + json.dumps(details, sort_keys=True),
                    file=sys.stderr,
                )
        elif raw_out:
            sys.stdout.write(raw_out)
    else:
        code = schema_check.main([*sys.argv[1:], "--db-url", db_url])
except SystemExit:
    raise
except Exception as exc:
    entry = "storage.main" if _use_storage else "schema_check.main"
    print(
        f"NOT EVALUATED - deploy blocked: {{entry}} raised " + repr(exc),
        file=sys.stderr,
    )
    sys.exit({EXIT_NOT_EVALUATED})
sys.exit(code)
"""

_DOKPLOY_NETWORK = "dokploy-network"
_VAULT_AGENT_SERVICE_KEY = "vault-agent"
_SSH_CONTROL_PATH = "/tmp/infra2-schema-gate-%r@%h:%p"


class SchemaGateError(RuntimeError):
    """The pre-deploy schema gate blocked, or could not run to a verdict.

    ``verdict`` is ``VERDICT_BLOCKED`` (exit 1), ``VERDICT_NOT_EVALUATED`` (exit 3) or
    ``VERDICT_NO_VERDICT`` (every other failure).
    """

    def __init__(self, message: str, *, verdict: str = VERDICT_NO_VERDICT) -> None:
        super().__init__(message)
        self.verdict = verdict


def verdict_for_exit(returncode: int) -> str:
    """The gate verdict for a non-zero check exit code."""
    if returncode == EXIT_BLOCKED:
        return VERDICT_BLOCKED
    if returncode == EXIT_NOT_EVALUATED:
        return VERDICT_NOT_EVALUATED
    return VERDICT_NO_VERDICT


def gate_applies(service: str) -> bool:
    """Whether this service declares a ``SchemaCheckSpec`` in ``_SCHEMA_CHECK_SPECS``.

    Every other service is not gated. A service with persistent enums needs a
    ``_SCHEMA_CHECK_SPECS`` entry first (``docs/onboarding/07.new-service-sop.md``).
    """
    return service in _SCHEMA_CHECK_SPECS


def schema_check_argv(service: str, spec: SchemaCheckSpec) -> list[str]:
    """The SDK ``schema_check`` options for ``spec``; ``_LAUNCHER`` adds ``--db-url``."""
    argv = ["--metadata", spec.metadata]
    for module in spec.imports:
        argv += ["--import", module]
    return [*argv, "--label", service]


def _ssh_args(host: str) -> list[str]:
    """The watchdog SSH convention (``tools/secrets_reconcile_check.py``,
    ``common/security/vault_self_refresh_audit.py``): explicit key/port/user when
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
    from common.deploy.compose_compile import load_stack

    # The compiled text names the vault-agent sidecar of an x-infra2 stack (infra2#1822).
    text = load_stack(compose_path).compiled
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
_ROLLBACK_CLASS_RE = re.compile(r"^(?:\[[^\]]+\]\s+)?ROLLBACK_CLASS:\s*(\S+)")


def _sanitize_db_url(text: str) -> str:
    """Mask credentials in database URLs (e.g. postgresql://user:pass@host -> postgresql://user:******@host)."""
    return _DB_PASSWORD_RE.sub(r"://\1:******@", text)


def _run_remote_check(
    host: str,
    *,
    image: str,
    service: str,
    spec: SchemaCheckSpec,
    database_url: str,
    runner,
    timeout: float,
) -> subprocess.CompletedProcess:
    docker_argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        _DOKPLOY_NETWORK,
        "--memory=1g",
        "--cpus=1",
        "--entrypoint",
        "python3",
        image,
        "-c",
        _LAUNCHER,
        *schema_check_argv(service, spec),
    ]
    try:
        return runner(
            [*_ssh_args(host), shlex.join(docker_argv)],
            input=f"{database_url}\n",
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # The exception text can carry the command; identify the failure type only.
        raise SchemaGateError(
            f"pre-deploy schema gate: the check of {service} in {image} on {host} "
            f"did not finish ({type(exc).__name__})"
        ) from None


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

    Raises :class:`SchemaGateError` on ANY other outcome. Its ``verdict`` tells a
    discrepancy (exit 1, ``BLOCKED``) from a check that compared nothing (exit 3,
    ``NOT EVALUATED``: no DB URL, a code-side import failure, or an image without the
    SDK ``schema_check``) from a transport failure (``NO VERDICT``). Every one of them
    means "do not proceed". Callers only invoke this when :func:`gate_applies` is true.
    """
    spec = _SCHEMA_CHECK_SPECS.get(service)
    if spec is None:
        raise SchemaGateError(
            f"{service}: pre-deploy schema gate has no registered backend image "
            "repository to run the check inside "
            "(common.deploy.schema_gate._SCHEMA_CHECK_SPECS)"
        )
    resolved_host = host or _ssh_host()
    vault_agent = _vault_agent_container(service, compose_path, env_suffix)
    database_url = _read_database_url(
        resolved_host, vault_agent, runner=runner, timeout=timeout
    )
    result = _run_remote_check(
        resolved_host,
        image=f"{spec.image_repository}:{image_ref}",
        service=service,
        spec=spec,
        database_url=database_url,
        runner=runner,
        timeout=timeout,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    # Parsed BEFORE the returncode check, on purpose: the SDK schema_check prints
    # ROLLBACK_CLASS on stdout for BOTH its passing and its blocked exit path (not for
    # NOT EVALUATED, exit 3, which never computed a comparison). The blocked case is
    # exactly when an operator most wants the class, so the exception carries it.
    rollback_class = ""
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        match = _ROLLBACK_CLASS_RE.match(line)
        if match:
            rollback_class = match.group(1).strip()
    if result.returncode != EXIT_OK:
        verdict = verdict_for_exit(result.returncode)
        suffix = f" (ROLLBACK_CLASS: {rollback_class})" if rollback_class else ""
        sanitized_output = _sanitize_db_url(output.strip()[-2000:])
        raise SchemaGateError(
            f"pre-deploy schema gate {verdict} {service} "
            f"(exit {result.returncode}){suffix}: {sanitized_output}",
            verdict=verdict,
        )
    if not rollback_class:
        # exit 0 with no parseable ROLLBACK_CLASS is not a real answer -- treat an
        # unparseable pass as blocking rather than trusting it (Rule 7's "never a
        # silent pass" applies to OUR parsing failures too).
        sanitized_output = _sanitize_db_url(output.strip()[-2000:])
        raise SchemaGateError(
            f"pre-deploy schema gate: {service} exited 0 but printed no ROLLBACK_CLASS "
            f"line: {sanitized_output}"
        )
    return rollback_class
