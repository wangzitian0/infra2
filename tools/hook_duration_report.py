#!/usr/bin/env python3
"""Report the hooks whose p95 duration passes 70% of their budget (#1823).

The truealpha `staging_gate` took 117 s, then 289 s, then more than 360 s, and nothing warned
before it timed out. Each stage record holds `checks[].seconds`. This tool reads the newest
stage records through the public `list_records` of `common/deploy/stage_record_reader.py`,
groups the seconds by hook and prints one line per hook with the p95, the budget and the ratio.

Report only: a hook over the threshold gives a `::warning::` annotation and a line in the step
summary. The exit code is 0 whatever the durations are. It is 1 only when the records cannot be
read, so that a broken report does not look like a quiet one.

The budget of a hook is derived from its `RuntimeHooksManifest` active check timeout sum
and `infra2_sdk.manifests.CELL_POLICIES` for the env and phase of its record.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python tools/hook_duration_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability.hook_duration_report import (  # noqa: E402, F401
    ENVS,
    LAST_SUBJECTS,
    MIN_SAMPLES,
    REPOS_YAML,
    THRESHOLD,
    annotations,
    cell_budgets,
    hook_budget_from_manifest,
    hook_budgets_from_manifests,
    load_hook_budgets,
    main,
    percentile,
    render,
    summarize,
)

if __name__ == "__main__":
    raise SystemExit(main())
