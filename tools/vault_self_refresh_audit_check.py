#!/usr/bin/env python3
"""CLI wrapper delegating to ``common.security.vault_self_refresh_audit`` (#531)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.security.vault_audit import (  # noqa: E402, F401
    audit_from_observations,
    inventory_ids_not_in_production,
)
from common.security.vault_self_refresh_audit import (  # noqa: E402, F401
    CAN_FAIL_BUT_REPORT_INFO,
    REPORT_ENV,
    collect_live_observations,
    confirmed_finding_pairs,
    load_report,
    main_check,
    read_unevaluated,
    run_check,
    unevaluated_keys,
)


def run(env: str = "production") -> dict[str, Any]:
    return run_check(
        env=env,
        collect_fn=lambda svcs, e: collect_live_observations(svcs, env=e),
        audit_fn=lambda svcs, obs, e: audit_from_observations(svcs, obs, env=e),
        excluded_fn=lambda: inventory_ids_not_in_production(),
    )


def main() -> int:
    return main_check(run_fn=run)


if __name__ == "__main__":
    sys.exit(main())
