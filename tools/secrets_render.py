#!/usr/bin/env python3
"""Vault Agent templates and policies, generated from environment manifests.

Every service's ``secrets.ctmpl`` and ``vault-policy.hcl`` are derived from the
manifest(s) that say who produces each variable (infra2-sdk contract v2): the
application's own manifest (checked out through the git submodule at the deployed
commit) plus, for platform services and stack-only additions, a manifest kept in
this repository next to the service. Nothing in a template is written by hand.

    uv run python tools/secrets_render.py --write        # regenerate every service
    uv run python tools/secrets_render.py --check        # CI: committed == generated
    uv run python tools/secrets_render.py --list         # what each service reads
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest
from infra2_sdk.secrets import render_agent_policy, render_agent_template

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Service:
    directory: str
    project: str
    service: str
    manifests: tuple[str, ...]
    source_env: str | None = None
    exclude_groups: tuple[str, ...] = ()
    # Hand-written files stay authoritative until the service is migrated; a listed
    # service is only *checked*, never rewritten, while ``generated`` is False.
    generated: bool = True
    # The IaC runner's policy is its *writer* identity (create/update on every project),
    # a bootstrap concern that no manifest describes; its template is still generated.
    policy: bool = True


SERVICES: tuple[Service, ...] = (
    Service(
        "bootstrap/06.iac_runner",
        "bootstrap",
        "iac_runner",
        ("bootstrap/06.iac_runner/env.manifest.json",),
        policy=False,
    ),
    Service(
        "platform/01.postgres",
        "platform",
        "postgres",
        ("platform/01.postgres/env.manifest.json",),
    ),
    Service(
        "platform/02.redis",
        "platform",
        "redis",
        ("platform/02.redis/env.manifest.json",),
    ),
    Service(
        "platform/03.minio",
        "platform",
        "minio",
        ("platform/03.minio/env.manifest.json",),
    ),
    Service(
        "platform/10.authentik",
        "platform",
        "authentik",
        ("platform/10.authentik/env.manifest.json",),
    ),
    Service(
        "platform/12.alerting",
        "platform",
        "alerting",
        ("platform/12.alerting/env.manifest.json",),
    ),
    Service(
        "platform/23.prefect",
        "platform",
        "prefect",
        ("platform/23.prefect/env.manifest.json",),
    ),
    Service(
        "platform/24.openpanel",
        "platform",
        "openpanel",
        ("platform/24.openpanel/env.manifest.json",),
    ),
    Service(
        "finance_report/finance_report/01.postgres",
        "finance_report",
        "postgres",
        ("finance_report/finance_report/01.postgres/env.manifest.json",),
    ),
    Service(
        "finance_report/finance_report/02.redis",
        "finance_report",
        "redis",
        ("finance_report/finance_report/02.redis/env.manifest.json",),
    ),
    Service(
        "truealpha/truealpha/01.postgres",
        "truealpha",
        "postgres",
        ("truealpha/truealpha/01.postgres/env.manifest.json",),
    ),
)


def load_manifest(path: str) -> EnvironmentManifest:
    return EnvironmentManifest.from_dict(
        json.loads((ROOT / path).read_text(encoding="utf-8"))
    )


def merged_manifest(service: Service) -> EnvironmentManifest:
    """Union of a service's manifests; the same variable must be declared identically."""
    fields: dict[str, EnvironmentField] = {}
    for path in service.manifests:
        for entry in load_manifest(path).fields:
            existing = fields.get(entry.env)
            if existing is not None and existing != entry:
                raise ValueError(
                    f"{service.directory}: {entry.env} declared differently in {path}"
                )
            fields.setdefault(entry.env, entry)
    return EnvironmentManifest(
        source=f"{service.project}/{service.service}", fields=tuple(fields.values())
    )


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
