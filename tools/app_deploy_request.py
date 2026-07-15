"""Validate an SDK deploy request and route it through infra2's deploy_v2 front door."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence

from libs.app_deploy_request import DeployPlan, make_plan


def execute_plan(plan: DeployPlan) -> int:
    from tools.deploy_v2 import main as deploy_v2_main

    return deploy_v2_main(plan.deploy_v2_args())


def _payload_from_env(name: str) -> str:
    payload = os.getenv(name, "")
    if not payload:
        raise ValueError(f"environment variable {name} is required")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "execute"))
    parser.add_argument("--payload-env", default="APP_DEPLOY_REQUEST_JSON")
    parser.add_argument("--sender", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--repo-root", default=".")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = make_plan(
            _payload_from_env(args.payload_env),
            sender=args.sender,
            domain=args.domain,
            timeout=args.timeout,
            repo_root=args.repo_root,
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
