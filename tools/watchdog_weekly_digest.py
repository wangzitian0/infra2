#!/usr/bin/env python3
"""CLI entrypoint for watchdog_weekly_digest."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability.digest import main as digest_main  # noqa: E402
from common.deploy.stage_record_reader import list_records  # noqa: E402


def main(argv: list[str]) -> int:
    """Run the digest with the stage records as the source of its Delivery section."""
    return digest_main(argv=argv, delivery_fetcher=list_records)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
