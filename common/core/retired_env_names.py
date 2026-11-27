"""Retired environment and service names, and a scanner that finds them.

One list defines which names are gone. `common/tests/test_retired_env_aliases.py`
fails when a manifest, compose file, Vault template, `.env.example` or deploy code
names one of them. Remove a name from this list only when it is back in use on purpose.
"""

from __future__ import annotations

import re
from pathlib import Path

# name -> where its replacement lives
RETIRED_NAMES: dict[str, str] = {
    "APP_ENV": "ENVIRONMENT",
    "AI_API_KEY": "LLM_API_KEY",
    "AI_BASE_URL": "LLM_BASE_URL",
    "AI_PROVIDER": "LLM_PROVIDER",
    "ZAI_API_KEY": "LLM_API_KEY",
    "RELEASE_MANIFEST_ID": "RELEASE_ID",
    "DATA_ENGINE_IMAGE_DIGEST": "IMAGE_DIGEST",
    "TRUEALPHA_CAPTURE_APPROVED_BY": "CAPTURE_APPROVED_BY",
    "TRUEALPHA_RELEASE_MANIFEST_ID": "RELEASE_ID",
    "TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST": "IMAGE_DIGEST",
    "TRUEALPHA_CONFIGURATION_SHA256": "CONFIGURATION_SHA256",
    # Writers stopped in #1781; readers stopped in #1802 (no live container or
    # Dokploy compose relies on it, read-only probes of 2026-11-22).
    "IAC_CONFIG_HASH": "CONFIGURATION_SHA256",
    "CANARY_S3_ENDPOINT_URL": "S3_ENDPOINT",
    "CANARY_S3_BUCKET": "S3_BUCKET",
    "CANARY_S3_ACCESS_KEY": "S3_ACCESS_KEY",
    "CANARY_S3_SECRET_KEY": "S3_SECRET_KEY",
    # MinIO was replaced by RustFS behind `platform-s3` (#930).
    "platform-minio": "platform-s3",
    "MINIO_S3_HOST_PORT": "S3_HOST_PORT",
}

# The guard governs what is deployed: service stacks, deploy code, and the two tables that
# map service names to containers. Analysis tools outside those trees are not configuration.
SCAN_ROOTS = (
    "platform",
    "finance_report",
    "truealpha",
    "bootstrap",
    "common/deploy",
    "common/security",
)
SCAN_FILES = ("common/core/environ.py", "common/core/facets.py")
SCAN_SUFFIXES = {".yaml", ".yml", ".json", ".ctmpl", ".hcl", ".py", ".sh", ".example"}
SKIP_DIRS = {"tests", "__pycache__", "node_modules", ".venv", ".git"}
SELF = Path(__file__).resolve()


def _pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


_PATTERNS = {name: _pattern(name) for name in RETIRED_NAMES}


def scan(root: Path) -> dict[str, list[str]]:
    """Return `{relative path: [retired names it contains]}` under `root`."""
    candidates: list[Path] = []
    for top in SCAN_ROOTS:
        base = root / top
        if base.is_dir():
            candidates.extend(sorted(base.rglob("*")))
    candidates.extend(root / rel for rel in SCAN_FILES)

    hits: dict[str, list[str]] = {}
    for path in candidates:
        if not path.is_file() or path.resolve() == SELF:
            continue
        if path.suffix not in SCAN_SUFFIXES:
            continue
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = sorted(name for name, pat in _PATTERNS.items() if pat.search(text))
        if found:
            hits[path.relative_to(root).as_posix()] = found
    return dict(sorted(hits.items()))
