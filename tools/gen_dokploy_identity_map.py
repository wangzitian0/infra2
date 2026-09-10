#!/usr/bin/env python3
"""Write the (project, compose) -> service_id map so it can travel without the tree.

`libs.service_registry` derives service identity by walking the deploy.py tree. The
alerting image does not ship that tree — it COPYs `libs` and `tools` and nothing else —
so inside `platform-alerting-probes` the registry is empty and every Dokploy compose
resolves to None: 32,236 "unregistered" log lines in 24 h, and every alert the watcher
raises labelled `infra/unregistered` instead of a real service (#608).

Baking the derivation (not the tree) keeps the image small and the identity real.

    python3 tools/gen_dokploy_identity_map.py            # rewrite libs/dokploy_identity_map.json
    python3 tools/gen_dokploy_identity_map.py --check    # fail if it has drifted (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.service_registry import (  # noqa: E402
    DOKPLOY_IDENTITY_MAP_PATH,
    _BOOTSTRAP_COMPOSE_IDS,
    service_attrs,
)


def build() -> dict[str, str]:
    """The map as the deploy.py tree declares it, right now."""
    out = {
        f"{meta.project}/{meta.service}": meta.service_id
        for meta in service_attrs().values()
    }
    out.update(
        {f"bootstrap/{name}": sid for name, sid in _BOOTSTRAP_COMPOSE_IDS.items()}
    )
    return dict(sorted(out.items()))


def render(mapping: dict[str, str]) -> str:
    return json.dumps(mapping, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing")
    args = parser.parse_args(argv)

    wanted = render(build())
    path = DOKPLOY_IDENTITY_MAP_PATH
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if args.check:
        if current == wanted:
            print(f"{path.name}: up to date ({len(build())} entries)")
            return 0
        print(
            f"{path.name} has drifted from the deploy.py tree; "
            "run `python tools/gen_dokploy_identity_map.py`",
            file=sys.stderr,
        )
        return 1
    path.write_text(wanted, encoding="utf-8")
    print(f"{path.name}: wrote {len(build())} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
