"""Validate an SDK deploy request and route it through infra2's deploy_v2 front door."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence

# libs.release_markers is pure git; libs.app_deploy_request pulls in infra2_sdk and is
# imported lazily, so `markers` runs in an ops job that installs neither (#650).
from libs.release_markers import marker_status


def execute_plan(plan, *, run=None) -> int:
    """Execute the primary service, then each companion the service spec declares.

    A companion (``libs.deploy_contract.ServiceSpec.companions``) is promoted at the
    same version_ref / iac_ref / type by the same request — truealpha/app carries
    truealpha/data_engine (truealpha#712). Companions are platform (iac_pinned)
    services, so ``--expected-sha`` — an app-image assertion — is dropped for them;
    the runner pins the release's image digest from ``version_ref`` instead. The
    first non-zero exit stops the sequence and is the request's exit code.
    """
    from libs.deploy_contract import service_spec

    if run is None:
        from tools.deploy_v2 import main as deploy_v2_main

        run = deploy_v2_main
    primary = plan.deploy_v2_args()
    code = run(primary)
    if code != 0:
        return code
    for companion in service_spec(plan.request.service).companions:
        code = run(_companion_args(primary, companion))
        if code != 0:
            return code
    return 0


def _companion_args(primary: list[str], companion: str) -> list[str]:
    args = list(primary)
    args[args.index("--service") + 1] = companion
    if "--expected-sha" in args:
        at = args.index("--expected-sha")
        del args[at : at + 2]
    return args


def _payload_from_env(name: str) -> str:
    payload = os.getenv(name, "")
    if not payload:
        raise ValueError(f"environment variable {name} is required")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "execute", "markers"))
    parser.add_argument("--payload-env", default="APP_DEPLOY_REQUEST_JSON")
    parser.add_argument("--sender", default="")
    parser.add_argument("--domain", default="")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--expected-iac-ref",
        default="",
        help="fail if the freshly validated plan no longer matches this prior coordinate",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "markers":
            # No request needed: this reports infra2's own two coordinates, so the daily
            # ops check can see the promotion lag without waiting for a release (#650).
            status = marker_status(repo_root=args.repo_root)
            print(status.line())
            return 1 if status.stale else 0

        from libs.app_deploy_request import make_plan

        if not args.sender or not args.domain:
            raise ValueError("--sender and --domain are required for plan and execute")
        plan = make_plan(
            _payload_from_env(args.payload_env),
            sender=args.sender,
            domain=args.domain,
            timeout=args.timeout,
            repo_root=args.repo_root,
        )
        if args.expected_iac_ref and plan.iac_ref != args.expected_iac_ref:
            raise ValueError(
                "validated iac_ref changed during the receiver run: "
                f"{args.expected_iac_ref!r} -> {plan.iac_ref!r}"
            )
        if args.action == "plan":
            print(json.dumps(plan.to_dict(), sort_keys=True))
            return 0
        return execute_plan(plan)
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"app deploy request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
