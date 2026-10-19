"""Git provenance: is a release ref reachable from reviewed main?

Canonical implementation lives in ``infra2_sdk.refs`` (#1270). Re-exported here
for ``tools/reconcile_iac_inputs.py`` and ``common.deploy.preflight``.
"""

from __future__ import annotations

from infra2_sdk.refs import assert_after_on_main

__all__ = ["assert_after_on_main"]
