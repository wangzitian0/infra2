#!/usr/bin/env python3
"""Generate Vault Agent templates and policies from environment manifests and verify them against committed files."""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # runnable as a script

from infra2_sdk.ci import validate_manifest_offline  # noqa: E402
from infra2_sdk.secrets import render_agent_policy, render_agent_template  # noqa: E402

from common.security.registry import (  # noqa: E402
    SERVICES,
    Service,
    load_manifest,
    manifest_file,
)
from common.security.registry import ROOT as _REGISTRY_ROOT  # noqa: E402
from common.security.registry import merged_manifest as _merged_manifest  # noqa: E402

ROOT = _REGISTRY_ROOT


def merged_manifest(service: Service):
    """Registry merge, rooted at this module's ROOT (tests re-point it)."""
    return _merged_manifest(service, root=ROOT)


__all__ = [
    "ROOT",
    "SERVICES",
    "VAULT_AGENT_HCL_TEMPLATE",
    "Service",
    "COMPILED_FILES",
    "check",
    "committed_files",
    "load_manifest",
    "manifest_file",
    "merged_manifest",
    "render",
    "render_agent_config",
    "write",
]

VAULT_AGENT_HCL_TEMPLATE = """\
vault {
  # VAULT_ADDR is set via container environment variable
  # vault-agent CLI automatically uses VAULT_ADDR env var when address is not specified
}
auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/vault/role_id"
      secret_id_file_path                 = "/vault/secret_id"
      remove_secret_id_file_after_reading = false
    }
  }

  sink {
    type = "file"
    config = {
      path = "/vault/.token"
    }
  }

  exit_on_err = true
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure = true
}

template {
  source      = "/etc/vault/secrets.ctmpl"
  destination = "/vault/secrets/.env"
  perms       = 0644
  # The template is generated from the service's manifest; a required key missing in
  # Vault must fail the render (visible, no container start) instead of rendering
  # `%!q(<nil>)`. Optional keys are omitted by the template itself (empty_ok).
  error_on_missing_key = true
}
"""


def render_agent_config() -> str:
    """Return the canonical Vault Agent configuration for all migrated services (#713)."""
    return VAULT_AGENT_HCL_TEMPLATE


def render(service: Service, *, root: Path | None = None) -> dict[str, str]:
    """The files of ``service``. ``root`` holds the manifests; the default is ``ROOT``.

    The config-drift check passes a directory that holds the manifests of a release ref.
    """
    manifest = (
        merged_manifest(service)
        if root is None
        else _merged_manifest(service, root=root)
    )
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
        ),
        "vault-agent.hcl": render_agent_config(),
    }
    if service.policy:
        outputs["vault-policy.hcl"] = render_agent_policy(manifest, **kwargs)
    return outputs


# The deploy compiles these two files into an x-infra2 stack (infra2#1822). Git keeps no copy.
COMPILED_FILES = ("secrets.ctmpl", "vault-agent.hcl")


def _is_x_infra2(service: Service, *, root: Path | None = None) -> bool:
    """True when ``<directory>/compose.yaml`` of ``service`` is an ``x-infra2`` source."""
    from common.deploy.compose_compile import has_x_infra2

    compose = (ROOT if root is None else root) / service.directory / "compose.yaml"
    return compose.is_file() and has_x_infra2(compose.read_text(encoding="utf-8"))


def committed_files(service: Service, *, root: Path | None = None) -> dict[str, str]:
    """The rendered files of ``service`` that git must hold, from manifests under ``root``.

    The render runs for every service, so a manifest error always fails. An ``x-infra2``
    stack commits only ``vault-policy.hcl``: the deploy compiles the other two files.
    """
    outputs = render(service, root=root)
    if _is_x_infra2(service, root=root):
        return {n: t for n, t in outputs.items() if n not in COMPILED_FILES}
    return outputs


def check(services: tuple[Service, ...] = SERVICES) -> list[str]:
    problems: list[str] = []
    for service in services:
        try:
            outputs = committed_files(service)
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
        for name, content in committed_files(service).items():
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
