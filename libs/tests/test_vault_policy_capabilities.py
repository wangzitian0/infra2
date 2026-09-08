"""The IaC Runner's Vault policy must admit every verb its writers use (#649, #629).

infra2-sdk >= 1.5.0 writes secrets with an HTTP PATCH (`application/merge-patch+json`) when
the path already exists, so only changed keys travel. KV v2 gates PATCH on its own `patch`
capability, which `create/read/update/list` does not imply: on 2026-09-08 every existing
truealpha service re-deploy failed `vault_permission_denied` for exactly that reason, and
the runner's own policy read clean because the missing verb is invisible unless you look for
it. This test looks for it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "bootstrap/06.iac_runner/vault-policy.hcl"
SDK_SECRETS = ROOT / "repos/infra2-sdk/src/infra2_sdk/secrets.py"

#: Every project whose service secrets the runner supplies during a deploy.
WRITTEN_PREFIXES = ("platform", "finance_report", "truealpha")


def _capabilities(policy: str) -> dict[str, list[str]]:
    blocks = re.findall(
        r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', policy
    )
    return {path: re.findall(r'"([a-z-]+)"', caps) for path, caps in blocks}


def test_every_written_secret_path_admits_patch_as_well_as_create_and_update() -> None:
    capabilities = _capabilities(POLICY.read_text())
    for project in WRITTEN_PREFIXES:
        path = f"secret/data/{project}/+/*"
        assert path in capabilities, f"{path} is not in the runner policy"
        granted = set(capabilities[path])
        assert {"create", "read", "update", "patch", "list"} <= granted, (
            f"{path}: {sorted(granted)}"
        )


def test_the_sdk_still_writes_with_patch_so_the_capability_is_still_required() -> None:
    """If the SDK ever stops using PATCH, this test says so instead of leaving a capability
    nobody can explain — the same reasoning that put `auth/token/renew-self` in the agent
    policies."""
    if not SDK_SECRETS.exists():  # the submodule is not checked out in every workspace
        return
    source = SDK_SECRETS.read_text()
    assert '"PATCH"' in source and "merge-patch+json" in source, (
        "infra2-sdk no longer writes with PATCH; drop the `patch` capability in the same PR"
    )
