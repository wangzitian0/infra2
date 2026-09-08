#!/usr/bin/env python3
"""Vault Agent templates and policies, generated from environment manifests.

Every migrated service's ``secrets.ctmpl`` (and, except for the IaC runner, its
``vault-policy.hcl``) is derived from the manifest(s) that say who produces each
variable (infra2-sdk contract v2): the application's own manifest (checked out through
the git submodule at the deployed commit) plus, for platform services and stack-only
additions, a manifest kept in this repository next to the service. The runner's policy
is its *writer* identity across projects, a bootstrap concern no manifest describes,
so it stays hand-written. Nothing else in a template is written by hand.

Required keys fail closed: each migrated service's ``vault-agent.hcl`` sets
``error_on_missing_key = true``, so a required value missing in Vault stops the render
instead of rendering ``%!q(<nil>)``; optional (``empty_ok``) keys are omitted by the
template itself.

    uv run python tools/secrets_render.py --write        # regenerate every service
    uv run python tools/secrets_render.py --check        # CI: committed == generated
    uv run python tools/secrets_render.py --list         # what each service reads
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # runnable as a script

from infra2_sdk.ci import validate_manifest_offline  # noqa: E402
from infra2_sdk.secrets import render_agent_policy, render_agent_template  # noqa: E402

from libs.secrets_registry import (  # noqa: E402
    SERVICES,
    Service,
    load_manifest,
    manifest_file,
)
from libs.secrets_registry import ROOT as _REGISTRY_ROOT  # noqa: E402
from libs.secrets_registry import merged_manifest as _merged_manifest  # noqa: E402

ROOT = _REGISTRY_ROOT


def merged_manifest(service: Service):
    """Registry merge, rooted at this module's ROOT (tests re-point it)."""
    return _merged_manifest(service, root=ROOT)


__all__ = [
    "ROOT",
    "SERVICES",
    "Service",
    "check",
    "load_manifest",
    "manifest_file",
    "merged_manifest",
    "render",
    "write",
]


def render(service: Service) -> dict[str, str]:
    manifest = merged_manifest(service)
    errors = validate_manifest_offline(manifest)
    if errors:
        raise ValueError(f"{service.directory}: " + "; ".join(errors))
    kwargs = {
        "project": service.project,
        "service": service.service,
        "source_env": service.source_env,
    }
    outputs = {
        "secrets.ctmpl": render_agent_template(
            manifest, exclude_groups=service.exclude_groups, **kwargs
        )
    }
    if service.policy:
        outputs["vault-policy.hcl"] = render_agent_policy(manifest, **kwargs)
    return outputs


def check(services: tuple[Service, ...] = SERVICES) -> list[str]:
    problems: list[str] = []
    for service in services:
        try:
            outputs = render(service)
        except (ValueError, FileNotFoundError) as error:
            problems.append(str(error))
            continue
        if not service.generated:
            continue
        for name, expected in outputs.items():
            target = ROOT / service.directory / name
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != expected:
                diff = difflib.unified_diff(
                    current.splitlines(),
                    expected.splitlines(),
                    f"{service.directory}/{name}",
                    "generated",
                    lineterm="",
                )
                problems.append("\n".join(diff))
    return problems


def write(services: tuple[Service, ...] = SERVICES) -> None:
    for service in services:
        if not service.generated:
            continue
        for name, content in render(service).items():
            (ROOT / service.directory / name).write_text(content, encoding="utf-8")
            print(f"wrote {service.directory}/{name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for service in SERVICES:
            manifest = merged_manifest(service)
            print(
                f"{service.project}/{service.service}: "
                + " ".join(f"{f.env}:{f.source}" for f in manifest.fields)
            )
        return 0
    if args.write:
        write()
        return 0
    problems = check()
    for problem in problems:
        print(problem)
    print(
        "secrets templates: ok"
        if not problems
        else f"secrets templates: {len(problems)} problem(s)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
