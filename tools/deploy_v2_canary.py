#!/usr/bin/env python3
"""deploy_v2 canary — the acceptance gate for the unified deploy primitive.

Exercises the WHOLE deploy_v2 path end to end against a real Dokploy: stand up a
throwaway preview alias via the unified front door (the ``canary`` deploy type), wait for
it to serve 200 at its public URL, then tear it (and its ephemeral DB) back down. A green
canary is the proof that ``deploy_v2(service, type, version_ref, iac_ref)`` actually
deploys — not just that the contract validates.

It deploys the ``finance_report/app`` service with ``type=canary``, which runs the chosen
code on a dedicated, reserved preview slot (``pr-<_CANARY_PR>`` — a number no real PR will
reuse), so it never touches staging/prod or a real PR's stack.

    run_canary(...)  -> deploy_v2(type=canary, version_ref) -> health 200 -> down (delete volumes)

The orchestration takes the same injected Dokploy client as the rest of the family, so its
logic is unit-testable with a fake (NO live calls in tests). The LIVE run needs real
Dokploy access and is operator-driven:

    python -m tools.deploy_v2_canary --version-ref main --iac-ref main --domain zitian.party

``--version-ref`` accepts any code surface (main | release/x.y | vX.Y.Z | <sha>); the
canary always runs it on the reserved slot. Add ``--keep`` to leave the stack up.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

import httpx  # transport errors from libs.dokploy surface as httpx exceptions

from tools.deploy_contract import DeployTarget
from tools.deploy_v2 import _CANARY_PR, deploy_v2
from tools.preview_lifecycle import down

_APP_SERVICE = "finance_report/app"


def _best_effort_down(*, domain: str, client, attempts: int = 3, _sleep=time.sleep) -> bool:
    """Tear the canary slot down, retrying transient control-plane errors.

    NEVER raises — teardown runs in a ``finally`` and must not mask a deploy error or crash
    the probe. Returns True if it cleaned up; on ultimate failure it returns False AND logs a
    loud warning, so a leaked stack is surfaced (not silent — the very gap that orphaned a
    pr-999 compose when Dokploy 502'd mid-teardown).
    """
    last = None
    for i in range(attempts):
        try:
            down("pr", _CANARY_PR, domain=domain, client=client)
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
    version_ref: str = "main",
    iac_ref: str = "main",
    wait: bool = True,
    teardown: bool = True,
    timeout: int = 600,
) -> CanaryResult:
    """Deploy the canary slot, assert health, then tear it down.

    ``version_ref`` is any code surface (main | release/x.y | vX.Y.Z | <sha>); ``iac_ref``
    pins infra2. deploy_v2 resolves both. Raises whatever deploy_v2 raises on a
    contract/red-line violation, or TimeoutError from the backend if the stack never goes
    healthy. Teardown runs in a ``finally`` so a failed/unhealthy deploy still cleans up
    its ephemeral stack.
    """
    torn_down = False
    res = None
    try:
        res = deploy_v2(
            service=_APP_SERVICE,
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
            torn_down = _best_effort_down(domain=domain, client=client)

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deploy_v2 acceptance canary")
    parser.add_argument(
        "--version-ref",
        default="main",
        help="app code surface to canary: main | release/x.y | vX.Y.Z | <sha>",
    )
    parser.add_argument(
        "--iac-ref",
        default="main",
        help="infra2 ref pinning the IaC: main | release/x.y | vX.Y.Z | <sha>",
    )
    parser.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the stack up (no teardown)"
    )
    parser.add_argument("--no-wait", action="store_true", help="do not health-check")
    parser.add_argument("--timeout", type=int, default=600, help="health-check seconds")
    args = parser.parse_args(argv)

    # Imported lazily so importing the module (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    client = get_dokploy(host=f"cloud.{args.domain}")
    try:
        result = run_canary(
            client=client,
            domain=args.domain,
            version_ref=args.version_ref,
            iac_ref=args.iac_ref,
            wait=not args.no_wait,
            teardown=not args.keep,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        # httpx.HTTPError covers Dokploy transport/auth/API failures from libs.dokploy —
        # an acceptance gate must exit cleanly (code 1 + one line), not dump a traceback.
        print(f"canary failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "ok": result.ok,
                "alias": result.alias,
                "url": result.url,
                "healthy": result.healthy,
                "torn_down": result.torn_down,
            }
        )
    )
    return 0 if result.ok or args.no_wait else 1


if __name__ == "__main__":
    raise SystemExit(main())
