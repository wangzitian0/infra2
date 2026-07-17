"""Lock the project portfolio index as GENERATED from each doc's own H1/Status, not
hand-maintained (#505).

Centralized index construction: each Infra-XXX doc's own title and Status line is the
single source; docs/project/README.md's Active/Archived tables are a projection of them
(tools/gen_project_index.py). Editing the README index by hand — or adding/renaming/
re-statusing a project doc without regenerating — fails here. Fix:
python tools/gen_project_index.py --write. This is what previously let Infra-004/005
drift to "In Progress" in the index while their own docs said "Completed", and let
Infra-010/011/014/016 go missing from the index entirely.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "tools/gen_project_index.py"


def test_project_readme_index_is_generated_from_docs() -> None:
    result = subprocess.run(
        [sys.executable, str(GEN)],  # --check mode (default): exit 1 if stale
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "docs/project/README.md portfolio index is out of sync with the project docs.\n"
        "Run: python tools/gen_project_index.py --write\n" + result.stdout + result.stderr
    )
