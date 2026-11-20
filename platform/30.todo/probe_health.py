#!/usr/bin/env python3
"""Verify Canary Todo /api/health endpoint for runtime hook slots (#1554)."""

from __future__ import annotations

import argparse
import calendar
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from infra2_sdk.deploy import default_http_get, poll_until_healthy

GATE_VERDICT_WAIT_SECONDS = 90
GATE_MAX_VERDICT_AGE_SECONDS = 180
GATE_REQUEST_TIMEOUT_SECONDS = 10
GATE_POLL_SECONDS = 10
SSO_PROTECTED_STATUSES = frozenset({301, 302, 303, 307, 308, 401, 403})
# The Canary image pin of the deployed release (common/deploy/canary_pin.py owns the format).
CANARY_PIN_RE = re.compile(
    r"ghcr\.io/wangzitian0/infra2-canary-todo:v(\d+\.\d+\.\d+)@sha256:[0-9a-f]{64}"
)


def pinned_canary_sdk(ref: str, runner=subprocess.run) -> str:
    """SDK version of the Canary image pin at ``ref``, or "" for a release before #247."""
    shown = runner(
        ["git", "show", f"{ref}:platform/30.todo/compose.yaml"],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return ""
    found = CANARY_PIN_RE.findall(shown.stdout)
    return found[0] if found else ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_summary_sso_probe(summary_url: str, timeout: float) -> int:
    """Issue unauthenticated POST /api/todos/summary without following redirects."""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(
        summary_url,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "canary-todo-probe/1.0",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as err:
        return int(err.code)


def _verdict_age_seconds(checked_at: object) -> float | None:
    try:
        stamp = calendar.timegm(time.strptime(str(checked_at), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
    return time.time() - stamp


def main(
    argv: Sequence[str] | None = None,
    *,
    summary_probe: Callable[[str, float], int] | None = None,
    pin_reader: Callable[[str], str] | None = None,
) -> int:
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
        "--verify-summary-sso",
        action="store_true",
        help="Verify unauthenticated POST /api/todos/summary stays protected by SSO.",
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
        default=GATE_REQUEST_TIMEOUT_SECONDS,
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
    if args.verify_summary_sso:
        probe = summary_probe or _default_summary_sso_probe
        summary_url = f"{args.base_url.rstrip('/')}/api/todos/summary"
        status_code = probe(summary_url, float(args.timeout))
        if status_code not in SSO_PROTECTED_STATUSES:
            raise AssertionError(
                f"unauthenticated POST {summary_url} returned {status_code}; expected SSO redirect/deny"
            )
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
        age = _verdict_age_seconds(canary.get("checked_at"))
        if age is None or age > GATE_MAX_VERDICT_AGE_SECONDS:
            raise AssertionError(f"canary verdict is stale or unreadable: {canary!r}")
        if payload.get("git_sha") != expected:
            raise AssertionError(f"git_sha {payload.get('git_sha')!r} != {expected}")
        # The train records sdk:X from the pin; the container must run X (infra2-sdk#247).
        pinned = (pin_reader or pinned_canary_sdk)(args.gate)
        if pinned and payload.get("sdk_version") != pinned:
            raise AssertionError(
                f"sdk_version {payload.get('sdk_version')!r} != pinned Canary SDK {pinned}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
