#!/usr/bin/env python3
"""deploy_v2 canary — the acceptance gate for the unified deploy primitive.

Exercises the WHOLE deploy_v2 path end to end against a real Dokploy: stand up a
throwaway preview alias via the unified front door (the ``canary`` deploy type), wait for
it to serve 200 at its public URL, then tear it (and its ephemeral DB) back down. A green
canary is the proof that ``deploy_v2(service, type, version_ref, iac_ref)`` actually
deploys — not just that the contract validates.

By default it deploys EVERY registry service declaring ``deploy_v2_canary = True`` on its
Deployer (#541 — today ``finance_report/app`` only; see :func:`canary_services`) with
``type=canary``, which runs the chosen code on a dedicated, reserved preview slot
(``pr-<_CANARY_PR>`` — a number no real PR will reuse), so it never touches staging/prod
or a real PR's stack. ``--service`` overrides the registry set with ONE explicit
preview-capable service (#522/#538) — anything registered in
``libs.deploy_env_config.preview_service_config`` works (e.g. ``truealpha/app``), even if
it has not opted into the scheduled canary via the Deployer flag.

    run_canary(...)  -> deploy_v2(type=canary, version_ref) -> health 200 -> down (delete volumes)

The orchestration takes the same injected Dokploy client as the rest of the family, so its
logic is unit-testable with a fake (NO live calls in tests). The LIVE run needs real
Dokploy access and is operator-driven:

    python -m tools.deploy_v2_canary --version-ref main --iac-ref main --domain zitian.party
    python -m tools.deploy_v2_canary --service truealpha/app --version-ref main --iac-ref main --domain zitian.party

``--version-ref`` accepts any code surface (main | vX.Y.Z | <sha>); the
canary always runs it on the reserved slot. Add ``--keep`` to leave the stack up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

import httpx  # transport errors from libs.dokploy surface as httpx exceptions
from infra2_sdk.delivery import (
    FailureDomain,
    StageResult,
    StageStatus,
    make_stage_result,
)

from libs.common import infra_domain
from libs.deploy.preview import down
from libs.deploy_contract import DeployTarget
from tools.deploy_v2 import _CANARY_PR, deploy_v2

_DEFAULT_SERVICE = "finance_report/app"


def canary_services() -> list[str]:
    """Registry services declaring scheduled deploy_v2-canary coverage (#541).

    Derived from each Deployer's ``deploy_v2_canary`` class attribute via the
    single registry derivation (service_attrs) — no hardcoded service id. Today
    that is finance_report/app only; truealpha auto-joins by flipping its own
    flag once its brand-new preview lane (#538) has been live-proven. Fails
    closed on an empty set: a scheduled canary that silently probes nothing is
    worse than a red one.
    """
    from libs.service_registry import service_attrs

    services = sorted(
        service_id
        for service_id, meta in service_attrs().items()
        if meta.deploy_v2_canary
    )
    if not services:
        raise RuntimeError(
            "no registry service declares deploy_v2_canary=True — the scheduled "
            "canary would silently prove nothing; refusing to run (see #541)"
        )
    return services

_SDK_FAILURE_DOMAIN = {
    "deploy-v2-control-plane": FailureDomain.DOKPLOY_CONTROL_PLANE,
    "deploy-v2-health": FailureDomain.DOCKER_RUNTIME,
    "deploy-v2-configuration": FailureDomain.CONFIGURATION,
    "deploy-v2-cleanup": FailureDomain.RESOURCE,
}

_EXTERNAL_FAILURE_DOMAINS = {
    "deploy-v2-control-plane",
    "deploy-v2-configuration",
}


def _best_effort_down(
    *, domain: str, client, service: str, attempts: int = 3, _sleep=time.sleep
) -> bool:
    """Tear the canary slot down, retrying transient control-plane errors.

    NEVER raises — teardown runs in a ``finally`` and must not mask a deploy error or crash
    the probe. Returns True if it cleaned up; on ultimate failure it returns False AND logs a
    loud warning, so a leaked stack is surfaced (not silent — the very gap that orphaned a
    pr-999 compose when Dokploy 502'd mid-teardown).
    """
    last = None
    for i in range(attempts):
        try:
            down("pr", _CANARY_PR, domain=domain, client=client, service=service)
            return True
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            last = exc
            if i < attempts - 1:
                _sleep(2**i)
    print(
        f"WARNING: canary teardown failed after {attempts} attempts ({last}); "
        f"possible leaked stack pr-{_CANARY_PR} — clean it up manually",
        file=sys.stderr,
    )
    return False


@dataclass(frozen=True)
class CanaryResult:
    ok: bool | None  # True/False after a health check; None when wait=False
    target: DeployTarget
    alias: str
    url: str
    healthy: bool | None
    torn_down: bool


def run_canary(
    *,
    client,
    domain: str,
    service: str = _DEFAULT_SERVICE,
    version_ref: str = "main",
    iac_ref: str = "main",
    wait: bool = True,
    teardown: bool = True,
    timeout: int = 600,
) -> CanaryResult:
    """Deploy the canary slot, assert health, then tear it down.

    ``service`` selects the preview-capable service under test (default
    ``finance_report/app``; any service registered in
    ``libs.deploy_env_config.preview_service_config`` works, e.g. ``truealpha/app``).
    ``version_ref`` is any code surface (main | vX.Y.Z | <sha>); ``iac_ref``
    pins infra2. deploy_v2 resolves both. Raises whatever deploy_v2 raises on a
    contract/red-line violation, or TimeoutError from the backend if the stack never goes
    healthy. Teardown runs in a ``finally`` so a failed/unhealthy deploy still cleans up
    its ephemeral stack.
    """
    torn_down = False
    res = None
    try:
        res = deploy_v2(
            service=service,
            deploy_type="canary",
            version_ref=version_ref,
            iac_ref=iac_ref,
            client=client,
            domain=domain,
            wait=wait,
            timeout=timeout,
        )
    finally:
        # Best-effort, never-raising teardown: if the deploy above raised (e.g. fast-fail on
        # an unpublished image), its error still propagates after we clean up — and a flaky
        # control plane can't leave the slot leaked silently.
        if teardown:
            torn_down = _best_effort_down(domain=domain, client=client, service=service)

    healthy = res.detail.get("healthy")
    ok = bool(healthy) if wait else None
    return CanaryResult(
        ok=ok,
        target=res.target,
        alias=res.detail.get("alias"),
        url=res.detail.get("url"),
        healthy=healthy,
        torn_down=torn_down,
    )


def failure_domain(exc: Exception) -> str:
    """Classify an EXCEPTION-path canary failure into a domain (route-canary-style taxonomy).

    Lets an alert say WHERE the deploy path broke. The full domain set (exact returned values):
    - ``deploy-v2-control-plane``  — Dokploy API / transport failure (httpx).
    - ``deploy-v2-health``         — deploy errored (composeStatus=error) or never converged.
    - ``deploy-v2-configuration``  — a bad ref / form / contract violation before any deploy.
    - ``deploy-v2-cleanup``        — healthy but the stack leaked (torn_down=false). NOT
                                     returned here (no exception) — ``main`` assigns it from
                                     the result. This function covers only the exception path.
    """
    if isinstance(exc, httpx.HTTPError):
        return "deploy-v2-control-plane"
    if isinstance(exc, (TimeoutError, RuntimeError)):
        return "deploy-v2-health"
    return "deploy-v2-configuration"  # ValueError: bad version_ref / form / contract


def make_canary_stage_result(
    *,
    domain: str | None,
    status: str | StageStatus,
    args,
    duration_ms: int,
    evidence_url: str = "",
    resolved_target: DeployTarget | None = None,
    skipped_reason: str = "",
) -> StageResult:
    """Build machine-readable canary evidence using the released SDK contract."""
    status_value = StageStatus(status)
    failure = (
        _SDK_FAILURE_DOMAIN.get(domain or "", FailureDomain.UNKNOWN)
        if status_value == StageStatus.FAIL
        else FailureDomain.NONE
    )
    target = (
        f"{resolved_target.service}@{resolved_target.code_version};"
        f"iac@{resolved_target.iac_ref}"
        if resolved_target is not None
        else f"{args.service}@{args.version_ref};iac@{args.iac_ref}"
    )
    return make_stage_result(
        source="tools.deploy_v2_canary",
        environment="pr",
        stage="deploy-smoke",
        target=target,
        status=status_value,
        duration_ms=duration_ms,
        failure_domain=failure,
        external_dependency=(domain or "") in _EXTERNAL_FAILURE_DOMAINS,
        skipped_reason=skipped_reason,
        evidence_url=evidence_url,
    )


def _run_url(env) -> str:
    server = env.get("GITHUB_SERVER_URL", "").rstrip("/")
    repository = env.get("GITHUB_REPOSITORY", "").strip("/")
    run_id = env.get("GITHUB_RUN_ID", "").strip()
    if not all((server, repository, run_id)):
        return ""
    return f"{server}/{repository}/actions/runs/{run_id}"


def alert_failure(env, *, domain: str, detail: str, args, duration_ms: int = 0) -> None:
    """Best-effort out-of-band alert that infra2's deploy-path probe is RED.

    Uses the SAME out-of-band Feishu path the watchdog uses (survives infra2 being down).
    NEVER raises — a missing webhook or a delivery error must not change the probe's exit
    code. A red deploy canary means the shared deploy_v2 path is broken, so a real
    staging/prod deploy would likely fail the same way.
    """
    from libs.alerting import deliver_out_of_band_text

    evidence = make_canary_stage_result(
        domain=domain,
        status=StageStatus.FAIL,
        args=args,
        duration_ms=duration_ms,
        evidence_url=_run_url(env),
    )
    text = (
        "🔴 deploy_v2 canary FAILED — infra2 deploy-path probe is RED\n"
        f"failure_domain: {domain}\n"
        f"stage_result: {json.dumps(evidence.to_dict(), sort_keys=True)}\n"
        f"version_ref={args.version_ref} iac_ref={args.iac_ref} domain={args.domain}\n"
        f"detail: {detail}\n"
        "→ the shared deploy_v2 path is broken; a real staging/prod deploy would likely "
        "fail the same way."
    )
    try:
        deliver_out_of_band_text(env, text)
        print("out-of-band alert delivered", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the probe
        print(f"WARNING: out-of-band alert delivery failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deploy_v2 acceptance canary")
    parser.add_argument(
        "--service",
        default=None,
        help="explicit preview-capable service to canary (any service registered in "
        "libs.deploy_env_config.preview_service_config, e.g. truealpha/app — #522/#538). "
        "Default: every registry service declaring deploy_v2_canary=True (#541), "
        "today finance_report/app only.",
    )
    parser.add_argument(
        "--version-ref",
        default="main",
        help="app code surface to canary: main | vX.Y.Z | <sha>",
    )
    parser.add_argument(
        "--iac-ref",
        default="main",
        help="infra2 ref pinning the IaC: main | vX.Y.Z | <sha>",
    )
    parser.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the stack up (no teardown)"
    )
    parser.add_argument("--no-wait", action="store_true", help="do not health-check")
    parser.add_argument("--timeout", type=int, default=600, help="health-check seconds")
    parser.add_argument(
        "--alert-on-failure",
        action="store_true",
        help="on failure, send an out-of-band Feishu alert (the deploy-path probe is RED). "
        "Use for the post-merge/scheduled probe; omit on PRs (a PR failure is CI feedback, "
        "not an infra incident).",
    )
    args = parser.parse_args(argv)

    # Imported lazily so importing the module (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    client = get_dokploy(host=f"cloud.{infra_domain()}")
    # --service = explicit single-service override (#538); default = every
    # registry service that opted into the scheduled canary (#541). One JSON
    # result line per service (today: exactly one — ops-checks' single-line
    # torn_down parse is unchanged until a second service enrolls).
    services = [args.service] if args.service else canary_services()
    overall_rc = 0
    for service in services:
        args.service = service  # stage-result/alert helpers read args.service
        overall_rc = max(overall_rc, _canary_one(client, args, service))
    return overall_rc


def _canary_one(client, args, service: str) -> int:
    """Run the canary for ONE service; returns its exit code (0 green)."""
    domain, detail, rc = None, None, 0
    started = time.monotonic()
    result = None
    try:
        result = run_canary(
            client=client,
            domain=args.domain,
            service=service,
            version_ref=args.version_ref,
            iac_ref=args.iac_ref,
            wait=not args.no_wait,
            teardown=not args.keep,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        # httpx.HTTPError covers Dokploy transport/auth/API failures from libs.dokploy —
        # the probe must exit cleanly (code 1 + one line), not dump a traceback.
        domain, detail, rc = failure_domain(exc), str(exc), 1
        print(f"canary failed [{domain}]: {exc}", file=sys.stderr)
    else:
        if not (result.ok or args.no_wait):  # deployed but never went healthy
            domain, detail, rc = "deploy-v2-health", f"healthy={result.healthy}", 1
        elif not (result.torn_down or args.keep):  # healthy but leaked its stack
            domain = "deploy-v2-cleanup"
            detail, rc = f"torn_down={result.torn_down} (possible leak)", 1

        duration_ms = round((time.monotonic() - started) * 1000)
        if rc != 0:
            stage_status = StageStatus.FAIL
            skipped_reason = ""
        elif args.no_wait:
            stage_status = StageStatus.SKIP
            skipped_reason = "health check disabled by --no-wait"
        else:
            stage_status = StageStatus.PASS
            skipped_reason = ""
        stage_result = make_canary_stage_result(
            domain=domain,
            status=stage_status,
            args=args,
            duration_ms=duration_ms,
            evidence_url=_run_url(os.environ),
            resolved_target=result.target,
            skipped_reason=skipped_reason,
        )
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "alias": result.alias,
                    "url": result.url,
                    "healthy": result.healthy,
                    "torn_down": result.torn_down,
                    "stage_result": stage_result.to_dict(),
                }
            )
        )

    if rc != 0 and args.alert_on_failure:
        duration_ms = round((time.monotonic() - started) * 1000)
        alert_failure(
            os.environ,
            domain=domain,
            detail=detail,
            args=args,
            duration_ms=duration_ms,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
