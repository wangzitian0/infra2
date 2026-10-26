"""Validate an SDK deploy request and route it through infra2's deploy_v2 front door."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from common.deploy.release_markers import marker_status

if TYPE_CHECKING:
    from common.deploy.app_deploy_request import DeployPlan


def execute_plan(plan: "DeployPlan", *, run=None) -> int:
    """Execute the primary service, then each companion the service spec declares.

    A companion (``common.deploy.contract.ServiceSpec.companions``) is promoted at the
    same version_ref / iac_ref / type by the same request — truealpha/app carries
    truealpha/data_engine (truealpha#712). Companions are platform (iac_pinned)
    services, so ``--expected-sha`` — an app-image assertion — is dropped for them;
    the runner pins the release's image digest from ``version_ref`` instead. The
    first non-zero exit stops the sequence and is the request's exit code.
    """
    from common.deploy.contract import service_spec

    if run is None:
        from common.deploy.v2 import main as deploy_v2_main

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


def check_preflight_canary_gate(
    plan: "DeployPlan",
    *,
    canary_job_ran: bool = False,
    canary_result: str | None = None,
) -> None:
    """Refuse when the workflow's canary pre-filter disagrees with the validated plan.

    app-deploy-request.yml decides whether the ``preflight_canary`` job runs before any
    plan exists, from the raw payload (truealpha#860: one job for a staging request). The
    plan is the authority (``DeployPlan.requires_preflight_canary``), so each side of the
    pre-filter is re-checked here, fail-closed:

    - ``canary_job_ran``: the canary job is running, so the plan must require a canary.
    - ``canary_result``: the canary job's ``needs.<job>.result`` as seen by the deploy
      job; a plan that requires a canary executes only after a successful one.
      ``None`` (flag not given) is an operator run outside the workflow: no check.
    """
    if canary_job_ran and not plan.requires_preflight_canary:
        raise ValueError(
            "the preflight_canary job ran for a request its validated plan does not "
            "canary: the workflow pre-filter has drifted from "
            "DeployPlan.requires_preflight_canary"
        )
    if (
        canary_result is not None
        and plan.requires_preflight_canary
        and canary_result != "success"
    ):
        raise ValueError(
            "the validated plan requires a green preflight canary, but the "
            f"preflight_canary job's result is {canary_result or 'empty'!r}: refusing to "
            "execute (the workflow pre-filter has drifted from "
            "DeployPlan.requires_preflight_canary)"
        )


def _utc_now() -> datetime:
    """The runner clock. A test replaces it."""
    return datetime.now(timezone.utc)


def _payload_from_env(name: str) -> str:
    payload = os.getenv(name, "")
    if not payload:
        raise ValueError(f"environment variable {name} is required")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "execute", "markers", "serving"))
    parser.add_argument("--payload-env", default="APP_DEPLOY_REQUEST_JSON")
    parser.add_argument("--service", default="finance_report/app")
    parser.add_argument("--env", default="prod", dest="environment")
    parser.add_argument("--sender", default="")
    parser.add_argument("--domain", default="")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--expected-iac-ref",
        default="",
        help="fail if the freshly validated plan no longer matches this prior coordinate",
    )
    parser.add_argument(
        "--request-created-at",
        default=None,
        help="the receiver run's created_at; a production request older than "
        "PRODUCTION_REQUEST_MAX_AGE_SECONDS is refused before the iac_ref check. "
        "Give it with --run-attempt; give neither for an operator run (no check)",
    )
    parser.add_argument(
        "--run-attempt",
        default=None,
        help="the receiver run's GITHUB_RUN_ATTEMPT; a production re-run is refused",
    )
    parser.add_argument(
        "--require-preflight-canary",
        action="store_true",
        help="the preflight canary job is running: fail unless the plan requires it",
    )
    parser.add_argument(
        "--preflight-canary-result",
        default=None,
        help="the preflight canary job's result; a plan that requires a canary is "
        "refused unless this is 'success'",
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

        if args.action == "serving":
            from common.core.environ import infra_domain
            from common.deploy.app_deploy_request import query_serving_release

            domain = args.domain or infra_domain()
            dokploy_client = None
            if os.getenv("DOKPLOY_API_KEY"):
                from common.deploy.dokploy_client import get_dokploy

                dokploy_client = get_dokploy(host=f"cloud.{infra_domain()}")
            serving = query_serving_release(
                args.service,
                args.environment,
                domain=domain,
                client=dokploy_client,
            )
            print(json.dumps(serving.to_dict(), sort_keys=True))
            return 0 if serving.healthy else 1

        from common.deploy.app_deploy_request import (
            check_request_age,
            make_plan,
            parse_sender_deadline,
            production_request_accept_until,
        )

        if not args.sender or not args.domain:
            raise ValueError("--sender and --domain are required for plan and execute")
        payload_text = _payload_from_env(args.payload_env)
        plan = make_plan(
            payload_text,
            sender=args.sender,
            domain=args.domain,
            timeout=args.timeout,
            repo_root=args.repo_root,
        )
        # The age and the re-run rule go first (#1232): an expired request is refused
        # for its age, even when a newer production marker also changed its iac_ref.
        # The two flags go together, so one of them alone fails closed.
        # Every request parses the sender deadline (#1363), so staging deploys use the
        # parse before production does. make_plan already proved the payload is an object.
        if args.request_created_at is not None or args.run_attempt is not None:
            sender_deadline = parse_sender_deadline(json.loads(payload_text))
            now = _utc_now()
            age = check_request_age(
                plan.request,
                created_at=args.request_created_at or "",
                run_attempt=args.run_attempt or "",
                now=now,
                sender_deadline=sender_deadline,
            )
            if age is not None:
                created = now - timedelta(seconds=age)
                accept_until = production_request_accept_until(created, sender_deadline)
                print(
                    f"request age {age:.0f} s, accepted until "
                    f"{accept_until:%Y-%m-%dT%H:%M:%SZ}",
                    file=sys.stderr,
                )
        if args.expected_iac_ref and plan.iac_ref != args.expected_iac_ref:
            raise ValueError(
                "validated iac_ref changed during the receiver run: "
                f"{args.expected_iac_ref!r} -> {plan.iac_ref!r}"
            )
        check_preflight_canary_gate(
            plan,
            canary_job_ran=args.require_preflight_canary,
            canary_result=args.preflight_canary_result,
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
