"""Project the App stack manifest copies from the App release that infra2 pins (#1822, #1823).

Each App stack holds a copy of the App environment manifests: ``env.manifest.json``.
The copy feeds the committed Vault Agent template and policy (``secrets_render``), the
secret supply and reconcile (``registry.store_keys``) and the compose contract check of
``verify_app_manifests``. This module makes each copy a generated projection of the App
manifests at one App release: it fetches the manifests at that release, merges them with
``infra2_sdk.runtime.merge_manifests`` (as ``registry.merged_manifest`` does), and writes
the result in the ``EnvironmentManifest.to_dict`` form.

``--check`` compares each copy with the projection at the tag in ``harness/repos.yaml``.
``--write`` rewrites each copy and the Vault files that ``secrets_render`` derives from it.
``sync_app_releases`` calls ``write_copies`` when it advances a tag, so the train PR
carries the regenerated copy.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from infra2_sdk.runtime import merge_manifests
from infra2_sdk.runtime.config_schema import EnvironmentManifest

from common.core.constants import APP_SOURCES, REPO_ROOT
from common.core.fetch_app_manifests import http_get
from common.deploy.app_contract import APP_UPSTREAM_MANIFESTS, parse_manifest
from common.security.registry import SERVICES, Service

ROOT = REPO_ROOT

#: Manifests that the infra2 compose supplies but the release render does not fetch.
#: The finance_report ``frontend`` compose service sets the five fields of this manifest
#: (``NEXT_PUBLIC_*`` and ``OPENPANEL_SCRIPT_URL``). None of them renders into the Vault
#: Agent template, so ``APP_UPSTREAM_MANIFESTS`` (the deploy path) does not list it.
COMPOSE_ONLY_MANIFESTS: dict[str, tuple[str, ...]] = {
    "finance_report/app": ("apps/frontend/required-env.manifest.json",),
}

Fetcher = Callable[[str], bytes]


def app_service_for(service_id: str) -> str:
    """The ``harness/repos.yaml`` App service that pins the release of ``service_id``."""
    return f"{service_id.split('/', 1)[0]}/app"


def upstream_paths(service_id: str) -> tuple[str, ...]:
    """The App repository paths that the copy of ``service_id`` projects, in merge order."""
    app_name = service_id.split("/", 1)[0]
    paths = tuple(
        path
        for owner, path in APP_UPSTREAM_MANIFESTS.get(app_name, ())
        if owner == service_id
    )
    if not paths:
        raise ValueError(f"no upstream manifests registered for {service_id!r}")
    return paths + COMPOSE_ONLY_MANIFESTS.get(service_id, ())


def projected_services(services: Sequence[Service] = SERVICES) -> tuple[Service, ...]:
    """The fixed-environment stacks whose copy is an App projection."""
    owners = {
        owner for entries in APP_UPSTREAM_MANIFESTS.values() for owner, _ in entries
    }
    return tuple(s for s in services if not s.preview and s.id in owners)


def manifest_url(repo_slug: str, ref: str, path: str) -> str:
    """The raw URL of ``path`` at ``ref``; a ``v*`` tag reads as ``refs/tags/<tag>``."""
    git_ref = f"refs/tags/{ref}" if ref.startswith("v") else ref
    return f"https://raw.githubusercontent.com/{repo_slug}/{git_ref}/{path}"


def project(
    service: Service,
    ref: str,
    *,
    repo_slug: str | None = None,
    fetcher: Fetcher | None = None,
) -> EnvironmentManifest:
    """The projection of the App manifests of ``service`` at App release ``ref``."""
    slug = repo_slug or APP_SOURCES.get(app_service_for(service.id), "")
    if not slug:
        raise ValueError(f"no source repository registered for {service.id!r}")
    fetch = fetcher or http_get
    parsed: list[tuple[str, EnvironmentManifest]] = []
    for path in upstream_paths(service.id):
        url = manifest_url(slug, ref, path)
        try:
            body = fetch(url)
        except SystemExit as exc:
            raise ValueError(
                f"failed to fetch manifest {path} from {url}: {exc}"
            ) from None
        parsed.append(
            (f"{path} at {url}", parse_manifest(body, label=f"{path} at {url}"))
        )
    return merge_manifests(parsed, source=f"infra2/{service.id}")


def render(manifest: EnvironmentManifest) -> str:
    """The committed text of a copy: ``EnvironmentManifest.to_dict`` key order, indent 2."""
    return json.dumps(manifest.to_dict(), indent=2) + "\n"


def _copy_path(service: Service) -> str:
    return service.manifests[0]


def check(
    tags: Mapping[str, str],
    *,
    root: Path = ROOT,
    services: Sequence[Service] = SERVICES,
    fetcher: Fetcher | None = None,
) -> list[str]:
    """One problem per copy that differs from its projection at the pinned App tag."""
    problems: list[str] = []
    for service in projected_services(services):
        tag = tags.get(app_service_for(service.id), "")
        if not tag:
            problems.append(f"{service.id}: harness/repos.yaml pins no release tag")
            continue
        expected = render(project(service, tag, fetcher=fetcher))
        target = root / _copy_path(service)
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current != expected:
            diff = difflib.unified_diff(
                current.splitlines(),
                expected.splitlines(),
                _copy_path(service),
                f"projection at {tag}",
                lineterm="",
            )
            problems.append("\n".join(diff))
    return problems


def write_copies(
    refs: Mapping[str, str],
    *,
    root: Path = ROOT,
    services: Sequence[Service] = SERVICES,
    fetcher: Fetcher | None = None,
) -> list[str]:
    """Rewrite the copy of each stack whose App is in ``refs``, then its Vault files.

    ``refs`` maps an App service (``finance_report/app``) to a release tag or commit SHA.
    Returns the relative paths whose content changed. The Vault files are the
    ``secrets_render`` outputs of every registered stack that reads a rewritten copy,
    the preview stacks included.
    """
    from common.security import secrets_render

    changed: list[str] = []
    rewritten: set[str] = set()
    for service in projected_services(services):
        ref = refs.get(app_service_for(service.id), "")
        if not ref:
            continue
        rel = _copy_path(service)
        text = render(project(service, ref, fetcher=fetcher))
        target = root / rel
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            changed.append(rel)
        rewritten.add(rel)
    for service in services:
        if not service.generated or not rewritten.intersection(service.manifests):
            continue
        for name, content in secrets_render.committed_files(service, root=root).items():
            rel = f"{service.directory}/{name}"
            target = root / rel
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8")
                changed.append(rel)
    return changed


def pinned_tags(root: Path = ROOT) -> dict[str, str]:
    """The App ``release_tag`` entries of ``harness/repos.yaml`` under ``root``."""
    from common.deploy.reconcile_iac_inputs import load_app_release_tags

    return load_app_release_tags(root)


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="Compare each copy with its projection."
    )
    mode.add_argument(
        "--write", action="store_true", help="Rewrite each copy and its Vault files."
    )
    args = parser.parse_args(argv)
    tags = pinned_tags(root)
    if args.write:
        for rel in write_copies(tags, root=root):
            print(f"wrote {rel}")
        return 0
    problems = check(tags, root=root)
    for problem in problems:
        print(problem)
    print(
        "app manifest copies: ok"
        if not problems
        else f"app manifest copies: {len(problems)} problem(s)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
