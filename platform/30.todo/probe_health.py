#!/usr/bin/env python3
"""Verify Canary Todo /api/health endpoint for runtime hook slots (#1554)."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Sequence

from infra2_sdk.deploy import default_http_get, poll_until_healthy

GATE_VERDICT_WAIT_SECONDS = 100
GATE_POLL_SECONDS = 10


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
        metavar="REF",
        default="",
        help="Require canary.ok true and git_sha equal to the commit of REF.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="HTTP request timeout in seconds.",
    )
    args = parser.parse_args(argv)

    url = f"{args.base_url.rstrip('/')}/api/health"
    deadline = time.monotonic() + GATE_VERDICT_WAIT_SECONDS
    while True:
        response = poll_until_healthy(
            url,
            http_get=default_http_get(timeout=float(args.timeout)),
            max_attempts=1,
            interval_seconds=0.0,
        )
        payload = json.loads(response.body)
        canary = payload.get("canary")
        # Gate mode waits for a verdict, not for a pass: a red verdict ends the wait.
        pending = (
            args.gate and isinstance(canary, dict) and not canary.get("checked_at")
        )
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"no canary verdict after {GATE_VERDICT_WAIT_SECONDS}s: {canary!r}"
            )
        time.sleep(GATE_POLL_SECONDS)
    if payload.get("ok") is not True:
        raise AssertionError(response.body)
    if args.require_service and payload.get("service") != args.require_service:
        raise AssertionError(response.body)
    if args.gate:
        expected = subprocess.run(
            ["git", "rev-parse", f"{args.gate}^{{commit}}"],
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
