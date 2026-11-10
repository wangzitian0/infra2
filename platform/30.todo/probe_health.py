#!/usr/bin/env python3
"""Verify Canary Todo /api/health endpoint for runtime hook slots (#1554)."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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

    base_url = (os.environ.get("TARGET_URL") or os.environ["APP_URL"]).rstrip("/")
    url = f"{base_url}/api/health"
    with urllib.request.urlopen(url, timeout=args.timeout) as resp:
        body = resp.read().decode("utf-8")
        status = resp.status
    payload = json.loads(body)
    if status != 200 or payload.get("ok") is not True:
        raise AssertionError(body)
    if args.require_service and payload.get("service") != args.require_service:
        raise AssertionError(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
