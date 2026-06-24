"""Lock the SSOT README topic index as GENERATED from MANIFEST, not hand-maintained.

Centralized index construction: MANIFEST is the single source; docs/ssot/README.md's topic
tables are a projection of it (tools/gen_ssot_index.py). Editing the README index by hand — or
adding a MANIFEST entry without regenerating — fails here. Fix: python tools/gen_ssot_index.py
--write. This kills the old README-restates-MANIFEST duplication (previously only asserted
equal, now generated).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "tools/gen_ssot_index.py"


def test_ssot_readme_index_is_generated_from_manifest() -> None:
    result = subprocess.run(
        [sys.executable, str(GEN)],  # --check mode (default): exit 1 if stale
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "docs/ssot/README.md SSOT index is out of sync with MANIFEST.\n"
        "Run: python tools/gen_ssot_index.py --write\n" + result.stdout + result.stderr
    )
