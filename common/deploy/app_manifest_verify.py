"""Run the real ``verify_app_manifests`` on every App stack that infra2 deploys (#1822, #1823).

``common/tests/test_deploy_v2.py`` replaces ``verify_app_manifests`` with a stub that returns
``{}``. A stub cannot see a manifest copy that lacks ``CONFIGURATION_SHA256``: release
v3.1.148 failed in staging for that reason. This check calls the real function, the same
call that the deploy makes, for each App stack, each deploy type and each of two release
sets:

* the App tags that ``harness/repos.yaml`` pins on this checkout (what the next staging deploy runs);
* the App tags that ``harness/repos.yaml`` pins at the newest ``production/vX.Y.Z`` marker (what
  production runs now, and what a rollback or a production promotion renders against).

``iac_ref`` is ``HEAD``: the stack manifests of this checkout. Any ``ValueError`` or
``SystemExit`` of the verifier is one problem line, and any problem fails the run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from common.core.constants import REPO_ROOT
from common.core.fetch_app_manifests import http_get
from common.deploy.app_contract import verify_app_manifests
from common.deploy.app_manifest_sync import app_service_for
from common.deploy.release_markers import PRODUCTION_MARKER_PREFIX

#: The App stacks that the deploy verifies. ``data_engine`` shares the ``truealpha/app`` release.
VERIFIED_SERVICES: tuple[str, ...] = (
    "finance_report/app",
    "truealpha/app",
    "truealpha/data_engine",
)
#: ``prod`` and ``staging`` render a fixed stack; ``production`` renders as a preview stack.
VERIFIED_DEPLOY_TYPES: tuple[str, ...] = ("staging", "prod")

Verifier = Callable[..., dict[str, str]]


def production_marker(
    root: Path, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> str:
    """The newest ``production/vX.Y.Z`` marker tag of ``root``, or an empty string."""
    listed = runner(
        [
            "git",
            "tag",
            "--list",
            f"{PRODUCTION_MARKER_PREFIX}v*.*.*",
            "--sort=-version:refname",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    names = listed.stdout.split() if listed.returncode == 0 else []
    return names[0] if names else ""


def production_app_tags(
    root: Path, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> dict[str, str]:
    """The App release tags that ``harness/repos.yaml`` holds at the newest production marker.

    Raises ``ValueError`` when the marker or its ``repos.yaml`` is unreadable: an unreadable
    source is a failed check, never an empty one.
    """
    from common.deploy.reconcile_iac_inputs import load_app_release_tags

    marker = production_marker(root, runner)
    if not marker:
        raise ValueError(
            f"no {PRODUCTION_MARKER_PREFIX}v*.*.* tag in this checkout; "
            f"fetch it: git fetch --depth=1 origin '+refs/tags/{PRODUCTION_MARKER_PREFIX}*"
            f":refs/tags/{PRODUCTION_MARKER_PREFIX}*'"
        )
    tags = load_app_release_tags(root, rev=marker, runner=runner)
    if not tags:
        raise ValueError(f"harness/repos.yaml at {marker} pins no App release tag")
    return tags


def _cached(fetch: Callable[[str], bytes]) -> Callable[[str], bytes]:
    """Fetch each URL once: the 12 calls read only 6 distinct manifest sets."""
    seen: dict[str, bytes] = {}

    def get(url: str) -> bytes:
        if url not in seen:
            seen[url] = fetch(url)
        return seen[url]

    return get


def verify_all(
    release_sets: Mapping[str, Mapping[str, str]],
    *,
    root: Path = REPO_ROOT,
    verifier: Verifier = verify_app_manifests,
    fetcher: Callable[[str], bytes] | None = None,
    contract_reader: Callable[[str, str], str] | None = None,
) -> list[str]:
    """One problem line per failed ``(release set, service, deploy type)``; empty when all pass.

    ``release_sets`` maps a label (``pinned``, ``production``) to ``{App service: release tag}``.
    A verifier that returns no rendered template for a service is also a problem: an empty
    answer is not a pass.
    """
    fetch = _cached(fetcher or http_get)
    problems: list[str] = []
    done: set[tuple[str, str, str]] = set()
    for label, tags in release_sets.items():
        for service in VERIFIED_SERVICES:
            tag = tags.get(app_service_for(service), "")
            if not tag:
                problems.append(f"{label} {service}: no App release tag")
                continue
            for deploy_type in VERIFIED_DEPLOY_TYPES:
                key = (service, tag, deploy_type)
                if key in done:
                    continue
                done.add(key)
                where = f"{label} {service}@{tag} {deploy_type}"
                try:
                    rendered = verifier(
                        service,
                        source_sha=tag,
                        deploy_type=deploy_type,
                        iac_ref="HEAD",
                        repo_root=root,
                        manifest_fetcher=fetch,
                        **(
                            {"contract_reader": contract_reader}
                            if contract_reader
                            else {}
                        ),
                    )
                except (ValueError, SystemExit, RuntimeError) as exc:
                    problems.append(f"{where}: {exc}")
                    continue
                if service not in rendered:
                    problems.append(f"{where}: the verifier rendered no template")
    return problems


def main(argv: list[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    from common.deploy.reconcile_iac_inputs import load_app_release_tags

    try:
        release_sets = {
            "pinned": load_app_release_tags(root),
            "production": production_app_tags(root),
        }
    except ValueError as exc:
        print(f"app manifests verify: {exc}")
        return 1
    problems = verify_all(release_sets, root=root)
    for problem in problems:
        print(problem)
    print(
        "app manifests verify: ok"
        if not problems
        else f"app manifests verify: {len(problems)} problem(s)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
