#!/usr/bin/env python3
"""deploy_v2 canary — the acceptance gate for the unified deploy primitive.

Exercises the WHOLE deploy_v2 path end to end against a real Dokploy: stand up a
throwaway preview alias via the unified front door, wait for it to serve 200 at its
public URL, then tear it (and its ephemeral DB) back down. A green canary is the proof
that ``deploy_v2(service, env, sub_domain, code_version, iac_ref)`` actually deploys —
not just that the contract validates.

It deploys the ``finance_report/app`` service to ``env=preview`` under a dedicated PR
alias (default ``pr-999`` — a number no real PR will reuse), so it never touches
staging/prod or a real PR's stack.

    run_canary(...)  -> deploy_v2(preview, pr-<N>) -> health 200 -> down (delete volumes)

The orchestration takes the same injected Dokploy client as the rest of the family, so
its logic is unit-testable with a fake (NO live calls in tests). The LIVE run needs real
Dokploy access and is operator-driven:

    python -m tools.deploy_v2_canary --code main --iac-ref <infra2-sha> --domain zitian.party

Add ``--keep`` to leave the stack up for inspection (skips teardown).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from tools.deploy_contract import DeployTarget
from tools.deploy_v2 import deploy_v2
from tools.preview_lifecycle import down

_APP_SERVICE = "finance_report/app"
# A reserved PR number for the canary alias — high enough that no real PR collides.
_CANARY_PR = 999


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
    code_version: str,
    iac_ref: str,
    pr_number: int = _CANARY_PR,
    wait: bool = True,
    teardown: bool = True,
    timeout: int = 600,
) -> CanaryResult:
    """Deploy the canary preview alias, assert health, then tear it down.

    ``code_version`` and ``iac_ref`` must be resolved 40-hex shas (the CLI resolves the
    surface inputs). Raises whatever deploy_v2 raises on a contract/red-line violation, or
    TimeoutError from the backend if the stack never goes healthy. Teardown runs in a
    ``finally`` so a failed/unhealthy deploy still cleans up its ephemeral stack.
    """
    torn_down = False
    res = None
    try:
        res = deploy_v2(
            service=_APP_SERVICE,
            env="preview",
            code_version=code_version,
            iac_ref=iac_ref,
            alias_kind="pr",
            alias_value=pr_number,
            client=client,
            domain=domain,
            wait=wait,
            timeout=timeout,
        )
    finally:
        if teardown:
            down("pr", pr_number, domain=domain, client=client)
            torn_down = True

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
    parser.add_argument("--code", required=True, help="app code surface: main | <sha>")
    parser.add_argument(
        "--iac-ref", required=True, help="infra2 ref pinning the IaC: branch | <sha>"
    )
    parser.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=_CANARY_PR,
        help=f"canary PR alias (default {_CANARY_PR})",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the stack up (no teardown)"
    )
    parser.add_argument("--no-wait", action="store_true", help="do not health-check")
    parser.add_argument("--timeout", type=int, default=600, help="health-check seconds")
    args = parser.parse_args(argv)

    # Resolve the surface inputs to shas: code against the app repo, iac_ref against infra2.
    from tools.resolve_deploy_ref import resolve_to_sha

    try:
        code_sha = resolve_to_sha(args.code)
        iac_sha = resolve_to_sha(
            args.iac_ref, repo="https://github.com/wangzitian0/infra2"
        )
    except (ValueError, RuntimeError) as exc:
        print(f"canary ref resolution failed: {exc}", file=sys.stderr)
        return 2

    # Imported lazily so importing the module (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    client = get_dokploy(host=f"cloud.{args.domain}")
    try:
        result = run_canary(
            client=client,
            domain=args.domain,
            code_version=code_sha,
            iac_ref=iac_sha,
            pr_number=args.pr,
            wait=not args.no_wait,
            teardown=not args.keep,
            timeout=args.timeout,
        )
    except (ValueError, RuntimeError, TimeoutError) as exc:
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
