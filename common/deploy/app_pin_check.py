"""Check each App pin that a pull request moves, with the checks of the train ``pre`` phase (#1644 Q2).

The ``pre`` phase of a Staging train refuses a release tag in two cases: an App release tag is
not on the ``main`` of its App, or it locks an infra2-sdk version that the Canary did not pass
in Staging. Both facts exist when a pull request moves the pin in ``harness/repos.yaml``. This
check calls the same two functions at that time, so the refusal comes before the merge.

Exit code 1: a check refuses a pin, or ``harness/repos.yaml`` is not readable. Exit code 0 with
``SKIPPED``: a remote source is not readable; the ``pre`` phase stays the gate. Exit code 0 with
``ok``: each moved pin passes, or the pull request moves no pin.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from common.core.constants import REPO_ROOT

#: What to do about a pin that locks an infra2-sdk version without a Canary pass.
CANARY_FIRST = (
    "Move the Canary slot of platform/30.todo/slots.yaml to that infra2-sdk version, "
    "pass one Staging train, then move the App pin."
)


def moved_pins(root: Path, base: str) -> dict[str, str]:
    """``{App service: release tag}`` of each pin that differs from the commit ``base``.

    Raises ``ValueError`` when one side holds no pin: an unreadable side is not "no pin moved".
    """
    from common.deploy.reconcile_iac_inputs import load_app_release_tags

    head = load_app_release_tags(root)
    if not head:
        raise ValueError("harness/repos.yaml pins no App release tag on this checkout")
    old = load_app_release_tags(root, rev=base) if base else {}
    if not old:
        raise ValueError(f"harness/repos.yaml is not readable at {base!r}")
    return {svc: tag for svc, tag in sorted(head.items()) if old.get(svc) != tag}


def check_pins(
    pins: Mapping[str, str],
    *,
    root: Path = REPO_ROOT,
    on_main: Callable[..., None] | None = None,
    sdk_verified: Callable[..., object] | None = None,
) -> tuple[list[str], list[str]]:
    """``(refusals, unread)``: one line for each refused pin and for each unreadable source."""
    from common.deploy.preflight import assert_app_version_ref_on_main
    from common.deploy.reconcile_iac_inputs import (
        UnverifiedSdkError,
        assert_app_sdk_versions_verified,
    )

    on_main = on_main or assert_app_version_ref_on_main
    sdk_verified = sdk_verified or assert_app_sdk_versions_verified
    refusals: list[str] = []
    unread: list[str] = []
    for svc, tag in pins.items():
        try:
            on_main(tag, "staging", service=svc)
        except ValueError as exc:
            refusals.append(f"{svc}@{tag}: {exc}")
        except Exception as exc:  # noqa: BLE001 - the compare API did not answer
            unread.append(f"{svc}@{tag}: {type(exc).__name__}: {exc}")
    try:
        sdk_verified(list(pins.items()), repo_root=root)
    except UnverifiedSdkError as exc:
        refusals.append(f"{exc}. {CANARY_FIRST}")
    except Exception as exc:  # noqa: BLE001 - a lock or the stage records are unread
        unread.append(f"infra2-sdk versions: {type(exc).__name__}: {exc}")
    return refusals, unread


def main(argv: list[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the commit before the change")
    args = parser.parse_args(argv)
    try:
        pins = moved_pins(root, args.base)
    except ValueError as exc:
        print(f"::error::app pin check: {exc}")
        return 1
    if not pins:
        print("app pin check: ok, no App pin moves")
        return 0
    refusals, unread = check_pins(pins, root=root)
    for line in refusals:
        print(f"::error::app pin check: the Staging train refuses this pin: {line}")
    for line in unread:
        print(f"::warning::app pin check: SKIPPED, a source is not readable: {line}")
    if refusals:
        return 1
    moved = ", ".join(f"{svc}@{tag}" for svc, tag in pins.items())
    print(f"app pin check: {'SKIPPED' if unread else 'ok'} ({moved})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
