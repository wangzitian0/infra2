#!/usr/bin/env python3
"""CLI entry point of the daily facet reconcile: ``python -m tools.facet_reconcile``.

The logic is in ``common/deploy/facet_reconcile.py`` (docs/ssot/core.harness.md
§5.3). This file is scheduled automation (AGENTS.md merge gate rule 6).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The `facet-reconcile` job of `.github/workflows/ops-checks.yml` imports these
# names from this module.
from common.deploy.facet_reconcile import (  # noqa: E402, F401
    confirmed_finding_pairs,
    main,
    read_unevaluated,
    run_all,
    unevaluated_prefixes,
)

if __name__ == "__main__":
    sys.exit(main())
