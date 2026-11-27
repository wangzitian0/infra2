"""Retired environment and service names must not come back (#1736 follow-up).

The scanner test proves the scanner itself: it finds a planted name, ignores a longer
name that only contains it, and skips tests and docs. The repository test then applies
the scanner to the real tree.
"""

from __future__ import annotations

from pathlib import Path

from common.core.retired_env_names import RETIRED_NAMES, scan

ROOT = Path(__file__).resolve().parents[2]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scanner_finds_planted_names_and_ignores_lookalikes(tmp_path: Path) -> None:
    _write(tmp_path, "truealpha/app/compose.yaml", "environment:\n  APP_ENV: staging\n")
    _write(tmp_path, "platform/x/env.manifest.json", '{"env": "ZAI_API_KEY"}')
    _write(
        tmp_path,
        "platform/y/compose.yaml",
        "target: http://platform-minio-staging:9000\n",
    )
    # Longer names that merely contain a retired name are not hits.
    _write(
        tmp_path,
        "common/deploy/contract.py",
        "_TRUEALPHA_APP_KEY = 'x'\nAPP_ENVIRONMENT = 1\n",
    )
    # Tests, docs and the table files are scanned only where the guard says so.
    _write(tmp_path, "platform/q/tests/test_x.py", "APP_ENV = 1\n")
    _write(tmp_path, "platform/z/README.md", "APP_ENV\n")
    _write(tmp_path, "common/core/facets.py", "T = 'http://platform-minio:9000'\n")
    _write(tmp_path, "common/core/harness_cli.py", "KEYS = {'APP_ENV'}\n")

    assert scan(tmp_path) == {
        "common/core/facets.py": ["platform-minio"],
        "platform/x/env.manifest.json": ["ZAI_API_KEY"],
        "platform/y/compose.yaml": ["platform-minio"],
        "truealpha/app/compose.yaml": ["APP_ENV"],
    }


def test_every_retired_name_has_a_replacement() -> None:
    assert all(RETIRED_NAMES.values())


def test_repository_names_no_retired_env_or_service_name() -> None:
    hits = scan(ROOT)
    report = "\n".join(
        f"  {path}: {', '.join(f'{n} -> {RETIRED_NAMES[n]}' for n in names)}"
        for path, names in hits.items()
    )
    assert hits == {}, f"retired names are still in use:\n{report}"
