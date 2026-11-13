#!/usr/bin/env python3
"""Verify Canary Todo /api/health endpoint for runtime hook slots (#1554)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Sequence

from infra2_sdk.deploy import default_http_get, poll_until_healthy


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url",
        help="Base service URL (for example https://todo-staging.zitian.party).",
    )
    parser.add_argument(
        "--require-service",
        default="",
        help="Require payload['service'] to equal this value.",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Require canary.ok true and git_sha equal to the commit of $EXPECTED_SHA.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="HTTP request timeout in seconds.",
    )
    args = parser.parse_args(argv)

    url = f"{args.base_url.rstrip('/')}/api/health"
    response = poll_until_healthy(
        url,
        http_get=default_http_get(timeout=float(args.timeout)),
        max_attempts=1,
        interval_seconds=0.0,
    )
    payload = json.loads(response.body)
    if payload.get("ok") is not True:
        raise AssertionError(response.body)
    if args.require_service and payload.get("service") != args.require_service:
        raise AssertionError(response.body)
    if args.gate:
        expected = subprocess.run(
            ["git", "rev-parse", f"{os.environ['EXPECTED_SHA']}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        canary = payload.get("canary")
        if not isinstance(canary, dict) or canary.get("ok") is not True:
            raise AssertionError(f"canary verdict is not ok: {canary!r}")
        if payload.get("git_sha") != expected:
            raise AssertionError(f"git_sha {payload.get('git_sha')!r} != {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
