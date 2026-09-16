"""Validate an SDK deploy request and route it through infra2's deploy_v2 front door."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

# libs.release_markers is pure git; libs.app_deploy_request pulls in infra2_sdk and is
# imported lazily, so `markers` runs in an ops job that installs neither (#650).
from libs.deploy_phase_log import phase
from libs.release_markers import marker_status

if TYPE_CHECKING:  # the annotation must not drag infra2_sdk into the markers path
    from libs.app_deploy_request import DeployPlan


# `run(args, on_triggered) -> exit_code` — the same shape for both the primary and
# every companion (a companion's own `on_triggered` is always a no-op: see execute_plan).
RunFn = Callable[[list[str], Callable[[], None]], int]


def execute_plan(plan: "DeployPlan", *, run: RunFn | None = None) -> int:
    """Execute the primary service, then each companion the service spec declares.

    A companion (``libs.deploy_contract.ServiceSpec.companions``) is promoted at the
    same version_ref / iac_ref / type by the same request — truealpha/app carries
    truealpha/data_engine (truealpha#712). Companions are platform (iac_pinned)
    services, so ``--expected-sha`` — an app-image assertion — is dropped for them;
    the runner pins the release's image digest from ``version_ref`` instead.

    Companion-parallel semantics (#truealpha critical-path, superseding the old
    "primary fully done, THEN each companion in turn" sequence): ``run`` is called as
    ``run(args, on_triggered)``, where ``on_triggered`` is a zero-arg callback the
    primary's own deploy_v2 call invokes once its trigger (Dokploy's ``deploy_compose``,
    or iac_runner's ``/deploy`` webhook) is ACCEPTED — before it waits for full health.
    The primary runs in a background thread; this function blocks only until EITHER that
    signal fires OR the primary thread finishes on its own, whichever comes first. If the
    primary finishes WITHOUT ever having signaled — a trigger-time failure (an evidence /
    gate / secrets-supply / Dokploy-API error, all of which happen before any trigger) —
    every companion is skipped and the primary's exit code is the request's exit code:
    "a failed primary TRIGGER skips the companion". Once the trigger is confirmed
    accepted, every companion starts concurrently with the (still-running) primary; this
    function then waits for the primary AND every companion to finish and reports the
    UNION of failures — a companion still runs (and its failure still counts) even if the
    primary later fails at a post-trigger stage (rollout wait / config verify / in-service
    verify), which the old first-non-zero-stops-the-sequence behavior would have masked
    by never reaching the companion at all.

    The default ``run`` (when the real ``tools.deploy_v2.main`` is used) always calls
    ``on_triggered`` at the right moment; a test double is free to call it immediately,
    late, or never (to exercise the "never triggered" skip path) — see
    libs/tests/test_app_deploy_request.py.
    """
    from libs.deploy_contract import service_spec

    if run is None:
        from tools.deploy_v2 import main as deploy_v2_main

        def run(args: list[str], on_triggered: Callable[[], None]) -> int:
            return deploy_v2_main(args, on_triggered=on_triggered)

    primary = plan.deploy_v2_args()
    companions = service_spec(plan.request.service).companions

    def run_primary(on_triggered: Callable[[], None]) -> int:
        phase(f"{plan.request.service}: primary: start")
        code = run(primary, on_triggered)
        phase(f"{plan.request.service}: primary: done (exit {code})")
        return code

    if not companions:
        return run_primary(lambda: None)

    triggered = threading.Event()
    primary_result: dict[str, int] = {}

    def primary_thread_target() -> None:
        primary_result["code"] = run_primary(triggered.set)

    primary_thread = threading.Thread(
        target=primary_thread_target, name="deploy-primary"
    )
    primary_thread.start()
    # Poll instead of a single blocking wait: we need to notice whichever of these two
    # happens FIRST — "triggered" (companions may start now) or "finished without ever
    # triggering" (a trigger-time failure; companions must never start) — and only
    # threading.Event.wait() with a repeated short timeout lets us also observe the
    # thread's own liveness in between.
    while not triggered.is_set() and primary_thread.is_alive():
        primary_thread.join(timeout=0.05)

    if not triggered.is_set():
        primary_thread.join()
        return primary_result["code"]

    companion_results: dict[str, int] = {}

    def run_companion(companion: str) -> None:
        phase(f"{companion}: companion: start")
        code = run(_companion_args(primary, companion), lambda: None)
        phase(f"{companion}: companion: done (exit {code})")
        companion_results[companion] = code

    companion_threads = [
        threading.Thread(
            target=run_companion,
            args=(companion,),
            name=f"deploy-companion-{companion}",
        )
        for companion in companions
    ]
    for companion_thread in companion_threads:
        companion_thread.start()
    primary_thread.join()
    for companion_thread in companion_threads:
        companion_thread.join()

    results = {plan.request.service: primary_result["code"]}
    results.update(
        {companion: companion_results[companion] for companion in companions}
    )
    failures = {name: code for name, code in results.items() if code != 0}
    if not failures:
        return 0
    print(
        "app deploy request: "
        f"{len(failures)} of {len(results)} deploy(s) failed: "
        + ", ".join(f"{name} (exit {code})" for name, code in failures.items()),
        file=sys.stderr,
    )
    return next(iter(failures.values()))


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
