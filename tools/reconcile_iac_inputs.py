#!/usr/bin/env python3
"""CLI entry point of the release train: ``python -m tools.reconcile_iac_inputs``.

The logic is in ``common/deploy/reconcile_iac_inputs.py``
(docs/ssot/core.harness.md §5.3). This file and its imports are release code
(AGENTS.md merge gate rule 4).
"""

from __future__ import annotations

# The `alert` job of `.github/workflows/reconcile-iac-inputs.yml` imports the
# two hook-result functions from this module.
from common.deploy.reconcile_iac_inputs import (  # noqa: F401
    hook_findings_from_result,
    main,
    resolve_workflow_failure_alert,
)

if __name__ == "__main__":
    raise SystemExit(main())
