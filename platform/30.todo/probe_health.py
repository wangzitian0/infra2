#!/usr/bin/env python3
"""Verify Canary Todo /api/health endpoint for runtime hook slots (#1554)."""

from __future__ import annotations

import argparse
import json
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
